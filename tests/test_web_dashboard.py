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
        assert len(hashed.split(":")) == 6  # scrypt:<salt>:<n>:<r>:<p>:<hash>
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

    def test_rejects_extreme_scrypt_params(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

        salt_hex = "aa" * 16
        hash_hex = "bb" * 32
        # n=2^20 far exceeds the 2^15 cap
        crafted = f"scrypt:{salt_hex}:{2**20}:8:2:{hash_hex}"
        assert not verify_password("anything", crafted)

    def test_rejects_extreme_r_param(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

        salt_hex = "aa" * 16
        hash_hex = "bb" * 32
        crafted = f"scrypt:{salt_hex}:{2**14}:64:2:{hash_hex}"
        assert not verify_password("anything", crafted)

    def test_rejects_extreme_p_param(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password

        salt_hex = "aa" * 16
        hash_hex = "bb" * 32
        crafted = f"scrypt:{salt_hex}:{2**14}:8:100:{hash_hex}"
        assert not verify_password("anything", crafted)

    def test_accepts_valid_v2_params(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            hash_password,
            verify_password,
        )

        pw = "test"
        hashed = hash_password(pw)
        assert verify_password(pw, hashed)


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

    def test_cleanup_all_expired_removes_stale_ips(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=5, window_seconds=0.01)
        rl.record_attempt("1.1.1.1")
        rl.record_attempt("2.2.2.2")
        rl.record_attempt("3.3.3.3")
        time.sleep(0.02)
        removed = rl.cleanup_all_expired()
        assert removed == 3
        assert len(rl._attempts) == 0

    def test_cleanup_all_expired_keeps_fresh_ips(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=5, window_seconds=60)
        rl.record_attempt("1.1.1.1")
        rl.record_attempt("2.2.2.2")
        removed = rl.cleanup_all_expired()
        assert removed == 0
        assert len(rl._attempts) == 2

    def test_cleanup_all_expired_enforces_max_tracked_ips(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=5, window_seconds=60)
        rl.MAX_TRACKED_IPS = 5
        for i in range(10):
            rl.record_attempt(f"10.0.0.{i}")
        # LRU eviction keeps the dict at MAX_TRACKED_IPS
        assert len(rl._attempts) == 5
        # Most recent IPs are retained, oldest evicted
        assert rl._attempts.get("10.0.0.9") is not None
        assert rl._attempts.get("10.0.0.0") is None

    def test_ip_cap_evicts_oldest_instead_of_bypassing(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter

        rl = RateLimiter(max_attempts=2, window_seconds=60)
        rl.MAX_TRACKED_IPS = 3
        rl.record_attempt("10.0.0.1")
        rl.record_attempt("10.0.0.2")
        rl.record_attempt("10.0.0.3")
        assert len(rl._attempts) == 3
        # New IP should evict oldest and still be recorded (not bypassed)
        rl.record_attempt("10.0.0.4")
        assert len(rl._attempts) == 3
        assert "10.0.0.4" in rl._attempts
        assert "10.0.0.1" not in rl._attempts
        # The new IP is tracked, so a second attempt should count
        rl.record_attempt("10.0.0.4")
        assert not rl.is_allowed("10.0.0.4")

    def test_gc_loop_cleans_rate_limiter(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="test", session_timeout=60)
        mgr.rate_limiter.window_seconds = 0.01
        mgr.rate_limiter.record_attempt("10.0.0.1")
        mgr.rate_limiter.record_attempt("10.0.0.2")
        time.sleep(0.02)
        mgr.cleanup_expired_sessions()
        assert len(mgr.rate_limiter._attempts) == 0


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

    def test_cleanup_expired_sessions_removes_stale(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="test", session_timeout=0.01)
        mgr.login("test", "10.0.0.1")
        mgr.login("test", "10.0.0.2")
        assert len(mgr.sessions) == 2
        time.sleep(0.05)
        removed = mgr.cleanup_expired_sessions()
        assert removed == 2
        assert len(mgr.sessions) == 0

    def test_cleanup_keeps_active_sessions(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="test", session_timeout=60)
        mgr.login("test", "10.0.0.1")
        mgr.login("test", "10.0.0.2")
        removed = mgr.cleanup_expired_sessions()
        assert removed == 0
        assert len(mgr.sessions) == 2

    def test_cleanup_mixed_expired_and_active(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="test", session_timeout=0.05)
        old_token = mgr.login("test", "10.0.0.1")
        time.sleep(0.06)
        new_token = mgr.login("test", "10.0.0.2")
        removed = mgr.cleanup_expired_sessions()
        assert removed == 1
        assert old_token not in mgr.sessions
        assert new_token in mgr.sessions

    def test_cleanup_returns_zero_when_empty(self):
        mgr = self._make_manager()
        assert mgr.cleanup_expired_sessions() == 0


class TestRateLimitConfig:
    def test_configurable_max_attempts_enforced(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(
            plaintext_password="testpass",
            rate_limit_max_attempts=2,
            rate_limit_window=60,
        )
        # Two wrong attempts permitted, recorded.
        assert mgr.login("wrong", "10.0.0.1") is None
        assert mgr.login("wrong", "10.0.0.1") is None
        # Now rate-limited: the correct password is also rejected.
        assert mgr.is_rate_limited("10.0.0.1")
        assert mgr.login("testpass", "10.0.0.1") is None

    def test_default_rate_limit_is_five(self):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="testpass")
        assert mgr.rate_limiter.max_attempts == 5
        assert mgr.rate_limiter.window_seconds == 60


class TestFailedLoginAudit:
    def test_failed_login_emits_warning_with_ip(self, caplog):
        import logging

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="testpass")
        with caplog.at_level(logging.WARNING):
            mgr.login("wrong", "203.0.113.7")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        audit = [r for r in warnings if "Failed dashboard login" in r.getMessage()]
        assert len(audit) == 1
        msg = audit[0].getMessage()
        assert "203.0.113.7" in msg
        assert "bad_password" in msg
        # Never log the password.
        assert "wrong" not in msg

    def test_second_immediate_failure_is_throttled(self, caplog):
        import logging

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="testpass")
        with caplog.at_level(logging.WARNING):
            mgr.login("wrong", "203.0.113.8")
            mgr.login("wrong", "203.0.113.8")

        audit = [
            r
            for r in caplog.records
            if "Failed dashboard login" in r.getMessage() and "203.0.113.8" in r.getMessage()
        ]
        # Only the first failure logs; the immediate second is suppressed.
        assert len(audit) == 1

    def test_throttle_reports_suppressed_count(self, caplog):
        import logging

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(plaintext_password="testpass")
        # First failure logs. Force the throttle window to elapse, then the
        # next failure should report the suppressed count.
        mgr.login("wrong", "203.0.113.9")
        mgr.login("wrong", "203.0.113.9")  # suppressed
        mgr.login("wrong", "203.0.113.9")  # suppressed
        mgr._audit_state["203.0.113.9"]["last_log_ts"] -= 100

        with caplog.at_level(logging.WARNING):
            caplog.clear()
            mgr.login("wrong", "203.0.113.9")

        audit = [
            r
            for r in caplog.records
            if "Failed dashboard login" in r.getMessage() and "203.0.113.9" in r.getMessage()
        ]
        assert len(audit) == 1
        assert "2 suppressed" in audit[0].getMessage()

    def test_rate_limited_reason_logged(self, caplog):
        import logging

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        mgr = AuthManager(
            plaintext_password="testpass",
            rate_limit_max_attempts=1,
        )
        mgr.login("wrong", "203.0.113.10")  # bad_password, logs
        # Push throttle window back so the next (rate-limited) attempt logs.
        mgr._audit_state["203.0.113.10"]["last_log_ts"] -= 100
        with caplog.at_level(logging.WARNING):
            caplog.clear()
            mgr.login("wrong", "203.0.113.10")

        audit = [
            r
            for r in caplog.records
            if "Failed dashboard login" in r.getMessage() and "203.0.113.10" in r.getMessage()
        ]
        assert len(audit) == 1
        assert "rate_limited" in audit[0].getMessage()


# --- SQLite session persistence tests ---


class TestSqliteSessionStore:
    def test_basic_operations(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)

        # set and get
        store["tok1"] = {"created_at": 1.0, "last_seen": 1.0, "remote_ip": "1.2.3.4"}
        assert store["tok1"]["remote_ip"] == "1.2.3.4"
        assert "tok1" in store
        assert len(store) == 1

        # delete
        del store["tok1"]
        assert "tok1" not in store
        assert len(store) == 0
        store.close()

    def test_persistence_across_instances(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store1 = SqliteSessionStore(db)
        store1["tok1"] = {"created_at": 1.0, "last_seen": 1.0, "remote_ip": "1.2.3.4"}
        store1.close()

        # Open a new instance — data should persist
        store2 = SqliteSessionStore(db)
        assert "tok1" in store2
        assert store2["tok1"]["remote_ip"] == "1.2.3.4"
        store2.close()

    def test_pop(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        store["tok1"] = {"created_at": 1.0, "last_seen": 1.0, "remote_ip": "x"}

        val = store.pop("tok1", None)
        assert val["remote_ip"] == "x"
        assert "tok1" not in store

        # pop missing key with default
        assert store.pop("missing", None) is None
        store.close()

    def test_get_default(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        assert store.get("missing") is None
        assert store.get("missing", "fallback") == "fallback"
        store.close()

    def test_items(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        store["a"] = {"v": 1}
        store["b"] = {"v": 2}
        items = dict(store.items())
        assert items["a"]["v"] == 1
        assert items["b"]["v"] == 2
        store.close()

    def test_iter(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        store["a"] = {"v": 1}
        store["b"] = {"v": 2}
        keys = list(store)
        assert set(keys) == {"a", "b"}
        store.close()

    def test_keyerror_on_missing(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        with pytest.raises(KeyError):
            _ = store["missing"]
        with pytest.raises(KeyError):
            del store["missing"]
        store.close()

    def test_checkpoint_truncates_wal(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import SqliteSessionStore

        db = str(tmp_path / "sessions.db")
        store = SqliteSessionStore(db)
        store["a"] = {"v": 1}
        # Should run without error and issue the truncating checkpoint pragma.
        store.checkpoint()
        store.close()
        # Checkpoint on a closed store is a no-op, not a crash.
        store.checkpoint()


class TestAuthManagerPersistent:
    def test_sessions_survive_restart(self, tmp_path):
        """Sessions persist across AuthManager instances via SQLite."""
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        mgr1 = AuthManager(
            plaintext_password="test",
            session_db_path=db,
        )
        token = mgr1.login("test", "127.0.0.1")
        assert token is not None
        assert mgr1.validate_token(token)

        # Create a new manager pointing to the same DB — simulates restart
        mgr2 = AuthManager(
            plaintext_password="test",
            session_db_path=db,
        )
        assert mgr2.validate_token(token)

    def test_eviction_works_with_sqlite(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        mgr = AuthManager(
            plaintext_password="test",
            max_sessions=2,
            session_db_path=db,
        )
        t1 = mgr.login("test", "10.0.0.1")
        time.sleep(0.01)
        mgr.login("test", "10.0.0.2")
        time.sleep(0.01)
        t3 = mgr.login("test", "10.0.0.3")

        # Oldest (t1) should be evicted
        assert not mgr.validate_token(t1)
        assert mgr.validate_token(t3)

    def test_cleanup_with_sqlite(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        mgr = AuthManager(
            plaintext_password="test",
            session_timeout=0.01,
            session_db_path=db,
        )
        mgr.login("test", "10.0.0.1")
        mgr.login("test", "10.0.0.2")
        time.sleep(0.05)
        removed = mgr.cleanup_expired_sessions()
        assert removed == 2
        assert len(mgr.sessions) == 0

    def test_logout_with_sqlite(self, tmp_path):
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        mgr = AuthManager(
            plaintext_password="test",
            session_db_path=db,
        )
        token = mgr.login("test", "127.0.0.1")
        mgr.logout(token)
        assert not mgr.validate_token(token)

    def test_last_seen_persists_with_sqlite(self, tmp_path):
        """Sliding-window: validate_token must write last_seen back to SQLite
        so an active session doesn't hard-expire session_timeout seconds
        after login regardless of use."""
        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        mgr = AuthManager(plaintext_password="test", session_db_path=db)
        token = mgr.login("test", "127.0.0.1")

        # Push last_seen back past the 60s write-throttle so the next
        # validate_token call is expected to persist a new value.
        session = mgr.sessions[token]
        session["last_seen"] -= 120
        mgr.sessions[token] = session
        pushed_back = mgr.sessions[token]["last_seen"]

        assert mgr.validate_token(token)

        # A fresh manager reading the same DB must see the advanced value.
        mgr2 = AuthManager(plaintext_password="test", session_db_path=db)
        assert mgr2.sessions[token]["last_seen"] > pushed_back


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

    def test_rejects_low_session_gc_interval(self, mock_app):
        config = {"enabled": True, "password": "test", "session_gc_interval": 10}
        with pytest.raises(ValueError, match="session_gc_interval"):
            self._make_plugin(mock_app, config)

    def test_accepts_valid_session_gc_interval(self, mock_app):
        config = {"enabled": True, "password": "test", "session_gc_interval": 60}
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_rejects_zero_rate_limit_max_attempts(self, mock_app):
        config = {"enabled": True, "password": "test", "rate_limit": {"max_attempts": 0}}
        with pytest.raises(ValueError, match="rate_limit.max_attempts"):
            self._make_plugin(mock_app, config)

    def test_rejects_zero_rate_limit_window(self, mock_app):
        config = {"enabled": True, "password": "test", "rate_limit": {"window_seconds": 0}}
        with pytest.raises(ValueError, match="rate_limit.window_seconds"):
            self._make_plugin(mock_app, config)

    def test_accepts_valid_rate_limit(self, mock_app):
        config = {
            "enabled": True,
            "password": "test",
            "rate_limit": {"max_attempts": 3, "window_seconds": 30},
        }
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_rejects_bad_allowed_networks(self, mock_app):
        config = {"enabled": True, "password": "test", "allowed_networks": ["not-a-cidr"]}
        with pytest.raises(ValueError, match="allowed_networks"):
            self._make_plugin(mock_app, config)

    def test_accepts_valid_allowed_networks(self, mock_app):
        config = {
            "enabled": True,
            "password": "test",
            "allowed_networks": ["127.0.0.1/32", "10.0.0.0/8"],
        }
        plugin = self._make_plugin(mock_app, config)
        assert plugin.plugin_name == "web_dashboard"

    def test_rejects_non_string_extra_hostnames(self, mock_app):
        config = {"enabled": True, "password": "test", "ssl": {"extra_hostnames": [123]}}
        with pytest.raises(ValueError, match="extra_hostnames"):
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

    mock_app.get_plugin = MagicMock(
        side_effect=lambda name: monitor if name == "system_monitor" else None
    )
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
        "allow_localhost_api": False,  # Disable for tests so auth is enforced
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

    def test_client_error_requires_csrf_header(self, client, event_loop):
        """POST /api/client_error without X-Requested-With is CSRF-rejected."""
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.post(
                "/api/client_error",
                json={"message": "boom"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 403

        event_loop.run_until_complete(_do())

    def test_client_error_unauthenticated_returns_401(self, client, event_loop):
        async def _do():
            resp = await client.post(
                "/api/client_error",
                json={"message": "boom"},
                headers={
                    "Accept": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            assert resp.status == 401

        event_loop.run_until_complete(_do())

    def test_client_error_accepted_with_auth_and_csrf(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            resp = await client.post(
                "/api/client_error",
                json={"message": "boom", "source": "/static/app.js", "line": 1},
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

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
            # Check that sensitive values are masked in plugins
            for name, cfg in data["data"]["plugins"].items():
                for key in ("password_hash", "password", "secret", "api_key"):
                    if key in cfg:
                        assert cfg[key] == "***"

        event_loop.run_until_complete(_do())

    def test_logout(self, client, event_loop):
        token = self._login(client, event_loop)

        async def _do():
            # Logout
            resp = await client.post(
                "/api/auth/logout",
                headers={"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"},
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

    def test_sw_js_headers(self, client, event_loop):
        async def _do():
            resp = await client.get("/sw.js")
            assert resp.status == 200
            assert resp.headers.get("Service-Worker-Allowed") == "/"
            assert resp.headers.get("Cache-Control") == "no-cache"
            csp = resp.headers.get("Content-Security-Policy", "")
            assert "worker-src 'self'" in csp

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


class TestTilesAuth:
    """Tiles are auth-gated (removed from PUBLIC_PREFIXES): anonymous tile
    fetches are rejected, logged-in clients reach the proxy handler."""

    @pytest.fixture
    def tile_plugin(self, dashboard_app):
        from unittest.mock import AsyncMock, MagicMock

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager
        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin

        config = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "password": "testpass",
            "allow_localhost_api": False,
            "tile_proxy": {"enabled": True},
        }
        plugin = WebDashboardPlugin(dashboard_app, config)
        plugin._start_time = time.time()
        plugin._active = True
        plugin._auth = AuthManager(plaintext_password="testpass")

        # Mock the upstream tile session so the handler returns bytes without
        # touching the network, and route writes to a tmp cache dir.
        import tempfile

        plugin._tile_cache_dir = tempfile.mkdtemp()
        plugin._tile_upstream = "https://tile.example/{z}/{x}/{y}.png"
        plugin._tile_max_bytes = 0
        plugin._tile_cache_bytes = 0

        resp_cm = MagicMock()
        resp_cm.__aenter__ = AsyncMock(
            return_value=MagicMock(status=200, read=AsyncMock(return_value=b"PNGDATA"))
        )
        resp_cm.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=resp_cm)
        plugin._tile_session = session
        return plugin

    @pytest.fixture
    def tile_app(self, tile_plugin):
        from reticulumpi.builtin_plugins.web_dashboard.server import create_app

        return create_app(tile_plugin)

    @pytest.fixture
    def event_loop(self):
        import asyncio

        loop = asyncio.new_event_loop()
        yield loop
        loop.close()

    @pytest.fixture
    def client(self, tile_app, event_loop):
        pytest.importorskip("aiohttp")
        from aiohttp.test_utils import TestClient, TestServer

        async def _make():
            server = TestServer(tile_app)
            client = TestClient(server)
            await client.start_server()
            return client

        client = event_loop.run_until_complete(_make())
        yield client
        event_loop.run_until_complete(client.close())

    def test_unauthenticated_tile_rejected(self, client, event_loop):
        async def _do():
            resp = await client.get(
                "/tiles/3/1/2.png",
                headers={"Accept": "application/json"},
                allow_redirects=False,
            )
            # Auth-gated now: 401 for JSON accept (or a 302 redirect for HTML).
            assert resp.status in (401, 302)

        event_loop.run_until_complete(_do())

    def test_authenticated_tile_reaches_handler(self, client, event_loop):
        async def _do():
            login = await client.post("/api/auth/login", json={"password": "testpass"})
            token = (await login.json())["data"]["token"]
            resp = await client.get(
                "/tiles/3/1/2.png",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status == 200
            assert resp.headers.get("Content-Type") == "image/png"
            body = await resp.read()
            assert body == b"PNGDATA"

        event_loop.run_until_complete(_do())


class TestIpAllowlist:
    """IP allowlist middleware (ships dark)."""

    def test_empty_list_allows_all(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from reticulumpi.builtin_plugins.web_dashboard.server import (
            ip_allowlist_middleware_factory,
        )

        plugin = MagicMock()
        plugin.config = {"allowed_networks": []}
        mw = ip_allowlist_middleware_factory(plugin)

        request = MagicMock()
        request.remote = "203.0.113.5"
        handler = AsyncMock(return_value="ok")
        result = asyncio.run(mw(request, handler))
        assert result == "ok"
        handler.assert_awaited_once()

    def test_non_member_gets_404(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        import aiohttp.web

        from reticulumpi.builtin_plugins.web_dashboard.server import (
            ip_allowlist_middleware_factory,
        )

        plugin = MagicMock()
        plugin.config = {"allowed_networks": ["10.0.0.0/8"]}
        mw = ip_allowlist_middleware_factory(plugin)

        request = MagicMock()
        request.remote = "203.0.113.5"
        handler = AsyncMock(return_value="ok")
        with pytest.raises(aiohttp.web.HTTPNotFound):
            asyncio.run(mw(request, handler))
        handler.assert_not_awaited()

    def test_member_allowed(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from reticulumpi.builtin_plugins.web_dashboard.server import (
            ip_allowlist_middleware_factory,
        )

        plugin = MagicMock()
        plugin.config = {"allowed_networks": ["10.0.0.0/8"]}
        mw = ip_allowlist_middleware_factory(plugin)

        request = MagicMock()
        request.remote = "10.1.2.3"
        handler = AsyncMock(return_value="ok")
        assert asyncio.run(mw(request, handler)) == "ok"

    def test_malformed_cidr_skipped(self):
        from unittest.mock import MagicMock

        from reticulumpi.builtin_plugins.web_dashboard.server import (
            ip_allowlist_middleware_factory,
        )

        plugin = MagicMock()
        plugin.config = {"allowed_networks": ["not-a-cidr", "10.0.0.0/8"]}
        # Factory should not raise; malformed entry is logged and skipped.
        mw = ip_allowlist_middleware_factory(plugin)
        assert mw is not None

    def test_loopback_allowlist_via_client(self, dashboard_app):
        """An allowlist of 127.0.0.1/32 lets loopback (the TestClient) through."""
        import asyncio

        from aiohttp.test_utils import TestClient, TestServer

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager
        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
        from reticulumpi.builtin_plugins.web_dashboard.server import create_app

        config = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 8080,
            "password": "testpass",
            "allow_localhost_api": False,
            "allowed_networks": ["127.0.0.1/32", "::1/128"],
        }
        plugin = WebDashboardPlugin(dashboard_app, config)
        plugin._start_time = time.time()
        plugin._active = True
        plugin._auth = AuthManager(plaintext_password="testpass")
        app = create_app(plugin)

        loop = asyncio.new_event_loop()
        try:

            async def _do():
                server = TestServer(app)
                client = TestClient(server)
                await client.start_server()
                try:
                    # login.html is public; the allowlist must let loopback reach it.
                    resp = await client.get("/login.html")
                    assert resp.status == 200
                finally:
                    await client.close()

            loop.run_until_complete(_do())
        finally:
            loop.close()


class TestSanGeneration:
    def test_cert_includes_expected_sans(self, tmp_path):
        pytest.importorskip("cryptography")
        from cryptography import x509

        from reticulumpi.builtin_plugins.web_dashboard.ssl_utils import (
            generate_self_signed_cert,
        )

        cert_path, key_path = generate_self_signed_cert(
            str(tmp_path),
            "TestNode",
            extra_sans=["pi.local", "192.168.1.50"],
        )
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns_names = set(san.get_values_for_type(x509.DNSName))
        ip_addrs = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}

        import socket

        assert "localhost" in dns_names
        assert "127.0.0.1" in ip_addrs
        assert socket.gethostname() in dns_names
        # extra_sans classified correctly: DNS vs IP.
        assert "pi.local" in dns_names
        assert "192.168.1.50" in ip_addrs


class TestHstsHeader:
    def _run_middleware(self, scheme):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from reticulumpi.builtin_plugins.web_dashboard.server import (
            security_headers_middleware,
        )

        request = MagicMock()
        request.scheme = scheme
        request.path = "/index.html"
        response = MagicMock()
        response.headers = {}
        response.content_type = "text/html"
        handler = AsyncMock(return_value=response)
        asyncio.run(security_headers_middleware(request, handler))
        return response.headers

    def test_hsts_present_on_https(self):
        headers = self._run_middleware("https")
        assert headers.get("Strict-Transport-Security") == "max-age=31536000"

    def test_hsts_absent_on_http(self):
        headers = self._run_middleware("http")
        assert "Strict-Transport-Security" not in headers


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


class TestSessionGcCheckpoint:
    def test_gc_loop_issues_wal_checkpoint(self, dashboard_plugin, tmp_path):
        """The periodic session GC sweep truncates the sessions WAL."""
        import asyncio
        from unittest.mock import MagicMock, patch

        from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager

        db = str(tmp_path / "sessions.db")
        dashboard_plugin._auth = AuthManager(plaintext_password="testpass", session_db_path=db)
        dashboard_plugin.config["session_gc_interval"] = 0

        checkpoint_spy = MagicMock(wraps=dashboard_plugin._auth.sessions.checkpoint)
        dashboard_plugin._auth.sessions.checkpoint = checkpoint_spy

        # Run exactly one sweep: the first sleep returns, then deactivate so the
        # while-loop exits on the next condition check.
        async def _fake_sleep(_interval):
            dashboard_plugin._active = False

        async def _drive():
            with patch("asyncio.sleep", _fake_sleep):
                await dashboard_plugin._session_gc_loop()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drive())
        finally:
            loop.close()

        checkpoint_spy.assert_called_once()


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


class TestTmpPasswordFile:
    def test_first_run_does_not_write_tmp_file(self, tmp_path):
        """The /tmp password file should NOT be created on first run."""
        import os

        from reticulumpi.builtin_plugins.web_dashboard.auth import (
            load_or_create_password_hash,
        )

        pw_file = "/tmp/reticulumpi-initial-password"
        if os.path.exists(pw_file):
            os.unlink(pw_file)

        load_or_create_password_hash(str(tmp_path))

        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
        from reticulumpi.event_bus import EventBus

        app = MagicMock()
        app.event_bus = EventBus()
        app.identity = MagicMock()
        app.identity.hash = b"\x01" * 16
        app.node_name = "TestNode"
        app.plugins = {}
        app.get_status.return_value = {"version": "test"}

        config = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 18080,
            "secret_dir": str(tmp_path / "fresh"),
        }

        WebDashboardPlugin(app, config)
        assert not os.path.exists(pw_file)

    def test_generated_password_written_to_file(self, tmp_path):
        """Auto-generated password is saved to a file, not logged."""
        import os
        from unittest.mock import patch

        from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password
        from reticulumpi.builtin_plugins.web_dashboard.plugin import WebDashboardPlugin
        from reticulumpi.event_bus import EventBus

        app = MagicMock()
        app.event_bus = EventBus()
        app.identity = MagicMock()
        app.identity.hash = b"\x01" * 16
        app.node_name = "TestNode"
        app.plugins = {}
        app.get_status.return_value = {"version": "test"}

        secret_dir = str(tmp_path / "secrets")
        config = {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 18081,
            "secret_dir": secret_dir,
        }

        plugin = WebDashboardPlugin(app, config)
        with (
            patch.object(plugin, "_setup_ssl", return_value=None),
            patch.object(plugin, "_start_thread"),
            patch(
                "reticulumpi.builtin_plugins.web_dashboard.server.create_app",
                return_value=MagicMock(),
            ),
        ):
            plugin.start()

        pw_file = os.path.join(secret_dir, "dashboard_password.txt")
        assert os.path.isfile(pw_file)
        assert oct(os.stat(pw_file).st_mode & 0o777) == "0o600"

        password = open(pw_file).read().strip()
        hash_file = os.path.join(secret_dir, "dashboard_secret")
        stored_hash = open(hash_file).read().strip()
        assert verify_password(password, stored_hash)
