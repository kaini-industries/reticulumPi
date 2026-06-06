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
    """Per-IP sliding window rate limiter."""

    MAX_TRACKED_IPS = 10_000

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def is_allowed(self, ip: str) -> bool:
        """Check if a login attempt from this IP is allowed."""
        ip = _normalize_ip(ip)
        now = time.monotonic()
        self._cleanup(ip, now)
        attempts = self._attempts.get(ip, [])
        return len(attempts) < self.max_attempts

    def record_attempt(self, ip: str) -> None:
        """Record a failed login attempt."""
        ip = _normalize_ip(ip)
        now = time.monotonic()
        self._cleanup(ip, now)
        if ip not in self._attempts and len(self._attempts) >= self.MAX_TRACKED_IPS:
            oldest_ip = min(
                self._attempts,
                key=lambda k: self._attempts[k][-1] if self._attempts[k] else 0,
            )
            del self._attempts[oldest_ip]
        self._attempts.setdefault(ip, []).append(now)

    def retry_after(self, ip: str) -> int:
        """Seconds until the oldest attempt expires (for Retry-After header)."""
        ip = _normalize_ip(ip)
        attempts = self._attempts.get(ip, [])
        if not attempts:
            return 0
        oldest = attempts[0]
        remaining = self.window_seconds - (time.monotonic() - oldest)
        return max(1, int(remaining))

    def cleanup_all_expired(self) -> int:
        """Sweep all IPs and remove entries with no unexpired attempts.

        Returns the number of IP entries removed.
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds
        expired_ips: list[str] = []
        for ip, timestamps in self._attempts.items():
            fresh = [t for t in timestamps if t > cutoff]
            if fresh:
                self._attempts[ip] = fresh
            else:
                expired_ips.append(ip)
        for ip in expired_ips:
            del self._attempts[ip]

        removed_by_cap = 0
        if len(self._attempts) > self.MAX_TRACKED_IPS:
            by_newest = sorted(
                self._attempts.items(),
                key=lambda kv: max(kv[1]),
            )
            excess = len(self._attempts) - self.MAX_TRACKED_IPS
            for ip, _ in by_newest[:excess]:
                del self._attempts[ip]
                removed_by_cap += 1

        return len(expired_ips) + removed_by_cap

    def _cleanup(self, ip: str, now: float) -> None:
        if ip in self._attempts:
            cutoff = now - self.window_seconds
            self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
            if not self._attempts[ip]:
                del self._attempts[ip]


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
            "CREATE TABLE IF NOT EXISTS sessions (  token TEXT PRIMARY KEY,  data TEXT NOT NULL)"
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
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (token, data) VALUES (?, ?)",
                (token, json.dumps(value)),
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


class AuthManager:
    """Manages password verification, session tokens, and rate limiting."""

    def __init__(
        self,
        password_hash: str | None = None,
        plaintext_password: str | None = None,
        session_timeout: float = 86400,
        max_sessions: int = 5,
        session_db_path: str | None = None,
    ):
        if password_hash:
            self._password_hash = password_hash
        elif plaintext_password:
            self._password_hash = hash_password(plaintext_password)
        else:
            raise ValueError("No password configured for web dashboard")

        self.session_timeout = session_timeout
        self.max_sessions = max_sessions
        if session_db_path:
            self.sessions: dict[str, dict[str, Any]] | SqliteSessionStore = SqliteSessionStore(
                session_db_path
            )
            log.info("Using persistent session store: %s", session_db_path)
        else:
            self.sessions = {}
        self.rate_limiter = RateLimiter()

    def login(self, password: str, remote_ip: str) -> str | None:
        """Verify password and create session. Returns token or None."""
        if not self.rate_limiter.is_allowed(remote_ip):
            return None

        if not verify_password(password, self._password_hash):
            self.rate_limiter.record_attempt(remote_ip)
            return None

        token = secrets.token_hex(32)
        now = time.time()
        self.sessions[token] = {
            "created_at": now,
            "last_seen": now,
            "remote_ip": remote_ip,
        }

        # Evict oldest sessions if over limit
        while len(self.sessions) > self.max_sessions:
            oldest_token = min(self.sessions, key=lambda t: self.sessions[t]["last_seen"])
            del self.sessions[oldest_token]

        return token

    def validate_token(self, token: str) -> bool:
        """Check if a session token is valid and update last_seen."""
        session = self.sessions.get(token)
        if not session:
            return False

        now = time.time()
        if now - session["last_seen"] > self.session_timeout:
            del self.sessions[token]
            return False

        # Re-assign via __setitem__ so SqliteSessionStore persists the slide —
        # __getitem__ there returns a fresh json.loads() copy, so mutating the
        # dict alone is a no-op. Throttled to avoid a SQLite write on every
        # API poll.
        if now - session["last_seen"] >= 60:
            session["last_seen"] = now
            self.sessions[token] = session

        return True

    def cleanup_expired_sessions(self) -> int:
        """Remove all expired sessions. Returns count of sessions removed.

        Called periodically by the web dashboard's background GC task so
        that sessions abandoned without an explicit logout don't accumulate
        in memory indefinitely.
        """
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
