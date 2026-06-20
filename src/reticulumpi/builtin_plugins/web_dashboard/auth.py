"""Authentication: password hashing, session token management, rate limiting."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


def _normalize_ip(ip: str) -> str:
    """Normalise an IP address so IPv4-mapped IPv6 variants share the same key.

    ``::ffff:127.0.0.1`` and ``127.0.0.1`` collapse to the same string,
    preventing rate-limit bypass via address format switching.
    """
    try:
        addr = ipaddress.ip_address(ip)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(addr)
    except ValueError:
        return ip


SECRET_FILENAME = "dashboard_secret"


def load_or_create_password_hash(secret_dir: str) -> tuple[str, str | None]:
    """Load password hash from file, or generate a new password and save the hash.

    Args:
        secret_dir: Directory to store the dashboard_secret file.

    Returns:
        (password_hash, generated_password) — generated_password is None if
        the hash was loaded from an existing file.
    """
    secret_dir = os.path.expanduser(secret_dir)
    os.makedirs(secret_dir, exist_ok=True)
    secret_path = os.path.join(secret_dir, SECRET_FILENAME)

    if os.path.isfile(secret_path):
        with open(secret_path, "r") as f:
            stored_hash = f.read().strip()
        if stored_hash:
            log.info("Loaded dashboard password hash from %s", secret_path)
            return stored_hash, None

    # Generate a new random password
    password = secrets.token_urlsafe(16)
    password_hash = hash_password(password)

    # Write hash atomically with restrictive permissions from the start
    fd = os.open(secret_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password_hash + "\n")

    log.info("Saved new dashboard password hash to %s", secret_path)
    return password_hash, password


def hash_password(password: str) -> str:
    """Hash a password with scrypt. Returns ``scrypt:<salt_hex>:<params>:<hash_hex>``.

    Uses n=2^14, r=8, p=2 (doubled parallelism over the original p=1).
    OpenSSL's scrypt enforces a 32 MB memory cap on many platforms (including
    Raspberry Pi), so n cannot exceed 2^14 with r=8.  Doubling *p* compensates
    by doubling the CPU time for each hash evaluation.

    The parameter block ``<params>`` is stored in the hash so that
    ``verify_password`` can handle both old and new formats transparently.
    """
    salt = os.urandom(16)
    n, r, p = 2**14, 8, 2
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt:{salt.hex()}:{n}:{r}:{p}:{dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored scrypt hash.

    Supports multiple formats for backward compatibility:
      - ``scrypt:<salt>:<hash>``              (v1: n=16384, r=8, p=1)
      - ``scrypt:<salt>:<n>:<r>:<p>:<hash>``  (v2: explicit params)
    """
    try:
        parts = stored_hash.split(":")
        if parts[0] != "scrypt":
            return False
        if len(parts) == 3:
            # Legacy v1 format
            salt = bytes.fromhex(parts[1])
            expected = bytes.fromhex(parts[2])
            n, r, p = 2**14, 8, 1
        elif len(parts) == 6:
            # Current v2 format: scrypt:<salt>:<n>:<r>:<p>:<hash>
            salt = bytes.fromhex(parts[1])
            n, r, p = int(parts[2]), int(parts[3]), int(parts[4])
            if n > 2**14 or r > 8 or p > 2:
                return False
            expected = bytes.fromhex(parts[5])
        else:
            return False
        dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
        return secrets.compare_digest(dk, expected)
    except Exception:
        log.warning("verify_password failed", exc_info=True)
        return False


class RateLimiter:
    """Per-IP exponential-backoff rate limiter.

    Each consecutive failed attempt from the same IP doubles the lockout
    window (starting from ``base_window``).  A successful login resets
    the counter for that IP.  This makes brute-force progressively more
    expensive without requiring persistent storage.
    """

    MAX_TRACKED_IPS = 10_000

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.base_window = window_seconds
        # {ip: {"attempts": [timestamps], "consecutive_failures": int}}
        self._state: dict[str, dict] = {}

    @property
    def window_seconds(self) -> int:
        """Base window — kept for backward compat with retry_after."""
        return self.base_window

    def _effective_window(self, ip: str) -> float:
        """Compute the exponential-backoff window for *ip*."""
        state = self._state.get(ip)
        if not state:
            return float(self.base_window)
        failures = state.get("consecutive_failures", 0)
        # Cap the multiplier at 2^8 = 256x (~4.25 h at base=60 s)
        multiplier = min(2 ** failures, 256)
        return float(self.base_window * multiplier)

    def is_allowed(self, ip: str) -> bool:
        """Check if a login attempt from this IP is allowed."""
        ip = _normalize_ip(ip)
        now = time.monotonic()
        self._cleanup(ip, now)
        state = self._state.get(ip)
        if not state:
            return True
        return len(state["attempts"]) < self.max_attempts

    def record_attempt(self, ip: str) -> None:
        """Record a failed login attempt."""
        ip = _normalize_ip(ip)
        now = time.monotonic()
        self._cleanup(ip, now)
        if ip not in self._state and len(self._state) >= self.MAX_TRACKED_IPS:
            oldest_ip = min(
                self._state,
                key=lambda k: (
                    self._state[k]["attempts"][-1]
                    if self._state[k]["attempts"]
                    else 0
                ),
            )
            del self._state[oldest_ip]
        if ip not in self._state:
            self._state[ip] = {"attempts": [], "consecutive_failures": 0}
        self._state[ip]["attempts"].append(now)
        self._state[ip]["consecutive_failures"] += 1

    def record_success(self, ip: str) -> None:
        """Reset backoff state for *ip* on successful login."""
        ip = _normalize_ip(ip)
        self._state.pop(ip, None)

    def retry_after(self, ip: str) -> int:
        """Seconds until the oldest attempt expires (for Retry-After)."""
        ip = _normalize_ip(ip)
        state = self._state.get(ip)
        if not state or not state["attempts"]:
            return 0
        oldest = state["attempts"][0]
        window = self._effective_window(ip)
        remaining = window - (time.monotonic() - oldest)
        return max(1, int(remaining))

    def cleanup_all_expired(self) -> int:
        """Sweep all IPs and remove entries with no unexpired attempts.

        Returns the number of IP entries removed.
        """
        now = time.monotonic()
        expired_ips: list[str] = []
        for ip, state in self._state.items():
            window = self._effective_window(ip)
            cutoff = now - window
            fresh = [t for t in state["attempts"] if t > cutoff]
            if fresh:
                state["attempts"] = fresh
            else:
                expired_ips.append(ip)
        for ip in expired_ips:
            del self._state[ip]

        removed_by_cap = 0
        if len(self._state) > self.MAX_TRACKED_IPS:
            by_newest = sorted(
                self._state.items(),
                key=lambda kv: max(kv[1]["attempts"])
                if kv[1]["attempts"]
                else 0,
            )
            excess = len(self._state) - self.MAX_TRACKED_IPS
            for ip, _ in by_newest[:excess]:
                del self._state[ip]
                removed_by_cap += 1

        return len(expired_ips) + removed_by_cap

    def _cleanup(self, ip: str, now: float) -> None:
        if ip in self._state:
            window = self._effective_window(ip)
            cutoff = now - window
            self._state[ip]["attempts"] = [
                t for t in self._state[ip]["attempts"] if t > cutoff
            ]
            if not self._state[ip]["attempts"]:
                del self._state[ip]


class SqliteSessionStore:
    """Dict-like session store backed by SQLite for persistence across restarts.

    Implements the subset of dict interface used by AuthManager: get, set,
    delete, pop, len, contains, items, and iteration over keys.
    """

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        os.chmod(db_path, 0o600)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "  token TEXT PRIMARY KEY,"
            "  data TEXT NOT NULL,"
            "  expires_at REAL"
            ")"
        )
        # Migrate: add expires_at column if missing (pre-existing DB)
        cols = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(sessions)")
        }
        if "expires_at" not in cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN expires_at REAL"
            )
        # Index for efficient expired-session cleanup
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at "
            "ON sessions (expires_at)"
        )
        self._conn.commit()

    def _is_closed(self) -> bool:
        return self._conn is None

    def __getitem__(self, token: str) -> dict[str, Any]:
        if self._is_closed():
            raise KeyError(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            raise KeyError(token)
        return json.loads(row[0])

    def __setitem__(self, token: str, value: dict[str, Any]) -> None:
        if self._is_closed():
            raise RuntimeError("session store closed")
        expires_at = value.get("expires_at")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(token, data, expires_at) VALUES (?, ?, ?)",
                (token, json.dumps(value), expires_at),
            )
            self._conn.commit()

    def __delitem__(self, token: str) -> None:
        if self._is_closed():
            raise RuntimeError("session store closed")
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._conn.commit()
        if cursor.rowcount == 0:
            raise KeyError(token)

    def __contains__(self, token: object) -> bool:
        if self._is_closed():
            return False
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM sessions WHERE token = ?", (token,)).fetchone()
        return row is not None

    def close(self) -> None:
        """Close the underlying database connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def __len__(self) -> int:
        if self._is_closed():
            return 0
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
        return row[0] if row else 0

    def __iter__(self):
        if self._is_closed():
            return iter([])
        with self._lock:
            rows = self._conn.execute("SELECT token FROM sessions").fetchall()
        return iter(r[0] for r in rows)

    def get(self, token: str, default: Any = None) -> Any:
        try:
            return self[token]
        except KeyError:
            return default

    def pop(self, token: str, *args: Any) -> Any:
        if self._is_closed():
            if args:
                return args[0]
            raise KeyError(token)
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                if args:
                    return args[0]
                raise KeyError(token)
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._conn.commit()
        return json.loads(row[0])

    def items(self):
        if self._is_closed():
            return []
        with self._lock:
            rows = self._conn.execute("SELECT token, data FROM sessions").fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def cleanup_expired(self) -> int:
        """Delete sessions whose ``expires_at`` is in the past.

        Uses the indexed ``expires_at`` column for an efficient single-pass
        DELETE instead of fetching + filtering every row.
        Returns the number of rows removed.
        """
        if self._is_closed():
            return 0
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL "
                "AND expires_at <= ?",
                (now,),
            )
            self._conn.commit()
        return cursor.rowcount

    def checkpoint(self) -> None:
        """Truncate the WAL so it does not grow unbounded on a tiny DB.

        Called from the dashboard's periodic session GC sweep. Best-effort:
        a busy checkpoint failure is harmless and simply retried next sweep.
        """
        if self._is_closed():
            return
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class AuthManager:
    """Manages password verification, session tokens, and rate limiting."""

    # Throttle for failed-login audit logging: at most one WARNING per IP per
    # this many seconds, with a count of suppressed attempts since the last
    # line. Capped to bound memory under a distributed login flood.
    _AUDIT_LOG_INTERVAL = 10.0
    _MAX_AUDIT_IPS = 10_000

    def __init__(
        self,
        password_hash: str | None = None,
        plaintext_password: str | None = None,
        session_timeout: float = 86400,
        max_sessions: int = 10,
        session_db_path: str | None = None,
        rate_limit_max_attempts: int = 5,
        rate_limit_window: int = 60,
        force_secure_cookie: bool = False,
        generated_pw_file: str | None = None,
    ):
        if password_hash:
            self._password_hash = password_hash
        elif plaintext_password:
            self._password_hash = hash_password(plaintext_password)
        else:
            raise ValueError("No password configured for web dashboard")

        # Checksum of the current password hash — stored in each session
        # so that validate_token can detect password rotation.
        self._password_hash_checksum = hashlib.sha256(
            self._password_hash.encode()
        ).hexdigest()

        self.session_timeout = session_timeout
        self.max_sessions = max_sessions
        self.force_secure_cookie = force_secure_cookie
        # Path to the generated password file; deleted after first login
        self._generated_pw_file = generated_pw_file
        if session_db_path:
            self.sessions: (
                dict[str, dict[str, Any]] | SqliteSessionStore
            ) = SqliteSessionStore(session_db_path)
            log.info("Using persistent session store: %s", session_db_path)
        else:
            self.sessions = {}
        self.rate_limiter = RateLimiter(
            max_attempts=rate_limit_max_attempts,
            window_seconds=rate_limit_window,
        )
        # Per-IP throttle state for failed-login audit logging:
        # {normalized_ip: {"last_log_ts": float, "suppressed_count": int}}
        self._audit_state: dict[str, dict[str, float]] = {}

        # Proportional last_seen update throttle (auth-03)
        self._last_seen_throttle = min(60, session_timeout / 10)

    def _audit_failed_login(self, remote_ip: str, reason: str) -> None:
        """Emit a throttled WARNING for a failed login. Never logs the password.

        At most one line per normalized IP per ``_AUDIT_LOG_INTERVAL`` seconds;
        the line carries the count of attempts suppressed since the previous
        line for that IP.
        """
        ip = _normalize_ip(remote_ip)
        now = time.monotonic()
        state = self._audit_state.get(ip)
        if state is not None and now - state["last_log_ts"] < self._AUDIT_LOG_INTERVAL:
            state["suppressed_count"] += 1
            return

        if state is None:
            # Evict the oldest tracked IP if at capacity (mirror RateLimiter).
            if len(self._audit_state) >= self._MAX_AUDIT_IPS:
                oldest_ip = min(
                    self._audit_state,
                    key=lambda k: self._audit_state[k]["last_log_ts"],
                )
                del self._audit_state[oldest_ip]
            suppressed = 0
        else:
            suppressed = int(state["suppressed_count"])

        self._audit_state[ip] = {"last_log_ts": now, "suppressed_count": 0}
        if suppressed:
            log.warning(
                "Failed dashboard login from %s (reason=%s, %d suppressed since last log)",
                ip,
                reason,
                suppressed,
            )
        else:
            log.warning("Failed dashboard login from %s (reason=%s)", ip, reason)

    def login(self, password: str, remote_ip: str) -> str | None:
        """Verify password and create session. Returns token or None."""
        if not self.rate_limiter.is_allowed(remote_ip):
            self._audit_failed_login(remote_ip, "rate_limited")
            return None

        if not verify_password(password, self._password_hash):
            self.rate_limiter.record_attempt(remote_ip)
            self._audit_failed_login(remote_ip, "bad_password")
            return None

        # Reset exponential backoff on success
        self.rate_limiter.record_success(remote_ip)

        token = secrets.token_hex(32)
        now = time.time()
        self.sessions[token] = {
            "created_at": now,
            "last_seen": now,
            "remote_ip": remote_ip,
            "password_hash_checksum": self._password_hash_checksum,
            "expires_at": now + self.session_timeout,
        }

        # IP-aware session eviction: prefer sessions from same IP
        while len(self.sessions) > self.max_sessions:
            norm_ip = _normalize_ip(remote_ip)
            same_ip = [
                (t, s) for t, s in self.sessions.items()
                if t != token
                and _normalize_ip(s.get("remote_ip", "")) == norm_ip
            ]
            if same_ip:
                victim = min(same_ip, key=lambda ts: ts[1]["last_seen"])
                del self.sessions[victim[0]]
            else:
                oldest_token = min(
                    (t for t in self.sessions if t != token),
                    key=lambda t: self.sessions[t]["last_seen"],
                )
                del self.sessions[oldest_token]

        # Delete the generated password file after first successful login
        if self._generated_pw_file:
            try:
                os.remove(self._generated_pw_file)
                log.info(
                    "Deleted generated password file: %s",
                    self._generated_pw_file,
                )
            except FileNotFoundError:
                pass
            except OSError:
                log.debug(
                    "Could not delete password file: %s",
                    self._generated_pw_file,
                    exc_info=True,
                )
            self._generated_pw_file = None

        return token

    def validate_token(self, token: str) -> bool:
        """Check if a session token is valid and update last_seen."""
        session = self.sessions.get(token)
        if not session:
            return False

        # Invalidate if the password was rotated since this session began
        stored_checksum = session.get("password_hash_checksum")
        if (
            stored_checksum is not None
            and stored_checksum != self._password_hash_checksum
        ):
            log.warning(
                "Password rotation detected — invalidating all sessions"
            )
            self._invalidate_all_sessions()
            return False

        now = time.time()
        if now - session["last_seen"] > self.session_timeout:
            del self.sessions[token]
            return False

        # Re-assign via __setitem__ so SqliteSessionStore persists the
        # slide.  Throttle is proportional to session_timeout (auth-03).
        if now - session["last_seen"] >= self._last_seen_throttle:
            session["last_seen"] = now
            session["expires_at"] = now + self.session_timeout
            self.sessions[token] = session

        return True

    def _invalidate_all_sessions(self) -> None:
        """Remove every active session (used on password rotation)."""
        tokens = list(self.sessions)
        for t in tokens:
            try:
                del self.sessions[t]
            except KeyError:
                pass

    def cleanup_expired_sessions(self) -> int:
        """Remove all expired sessions. Returns count of sessions removed.

        Called periodically by the web dashboard's background GC task so
        that sessions abandoned without an explicit logout don't accumulate
        in memory indefinitely.

        When backed by SqliteSessionStore the indexed ``expires_at``
        column is used for an efficient single-pass DELETE.
        """
        # Fast path: SqliteSessionStore with indexed expires_at column
        if hasattr(self.sessions, "cleanup_expired"):
            removed = self.sessions.cleanup_expired()
            self.rate_limiter.cleanup_all_expired()
            return removed

        now = time.time()
        expired = [
            token
            for token, session in self.sessions.items()
            if now - session["last_seen"] > self.session_timeout
        ]
        for token in expired:
            del self.sessions[token]
        self.rate_limiter.cleanup_all_expired()
        return len(expired)

    def logout(self, token: str) -> None:
        """Invalidate a session token."""
        self.sessions.pop(token, None)

    def is_rate_limited(self, remote_ip: str) -> bool:
        """Check if an IP is currently rate-limited."""
        return not self.rate_limiter.is_allowed(remote_ip)

    def get_retry_after(self, remote_ip: str) -> int:
        """Get Retry-After seconds for a rate-limited IP."""
        return self.rate_limiter.retry_after(remote_ip)
