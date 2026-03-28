"""Authentication: password hashing, session token management, rate limiting."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Any

log = logging.getLogger(__name__)

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

    # Write hash to file (restrictive permissions)
    with open(secret_path, "w") as f:
        f.write(password_hash + "\n")
    os.chmod(secret_path, 0o600)

    log.info("Saved new dashboard password hash to %s", secret_path)
    return password_hash, password


def hash_password(password: str) -> str:
    """Hash a password with scrypt. Returns 'scrypt:<salt_hex>:<hash_hex>'."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a stored scrypt hash."""
    try:
        parts = stored_hash.split(":")
        if len(parts) != 3 or parts[0] != "scrypt":
            return False
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
        dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


class RateLimiter:
    """Per-IP sliding window rate limiter."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 60):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def is_allowed(self, ip: str) -> bool:
        """Check if a login attempt from this IP is allowed."""
        now = time.monotonic()
        self._cleanup(ip, now)
        attempts = self._attempts.get(ip, [])
        return len(attempts) < self.max_attempts

    def record_attempt(self, ip: str) -> None:
        """Record a failed login attempt."""
        now = time.monotonic()
        self._cleanup(ip, now)
        self._attempts.setdefault(ip, []).append(now)

    def retry_after(self, ip: str) -> int:
        """Seconds until the oldest attempt expires (for Retry-After header)."""
        attempts = self._attempts.get(ip, [])
        if not attempts:
            return 0
        oldest = attempts[0]
        remaining = self.window_seconds - (time.monotonic() - oldest)
        return max(1, int(remaining))

    def _cleanup(self, ip: str, now: float) -> None:
        if ip in self._attempts:
            cutoff = now - self.window_seconds
            self._attempts[ip] = [t for t in self._attempts[ip] if t > cutoff]
            if not self._attempts[ip]:
                del self._attempts[ip]


class AuthManager:
    """Manages password verification, session tokens, and rate limiting."""

    def __init__(
        self,
        password_hash: str | None = None,
        plaintext_password: str | None = None,
        session_timeout: float = 86400,
        max_sessions: int = 5,
    ):
        if password_hash:
            self._password_hash = password_hash
        elif plaintext_password:
            self._password_hash = hash_password(plaintext_password)
        else:
            raise ValueError("No password configured for web dashboard")

        self.session_timeout = session_timeout
        self.max_sessions = max_sessions
        self.sessions: dict[str, dict[str, Any]] = {}
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
            oldest_token = min(
                self.sessions, key=lambda t: self.sessions[t]["last_seen"]
            )
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

        session["last_seen"] = now
        return True

    def logout(self, token: str) -> None:
        """Invalidate a session token."""
        self.sessions.pop(token, None)

    def is_rate_limited(self, remote_ip: str) -> bool:
        """Check if an IP is currently rate-limited."""
        return not self.rate_limiter.is_allowed(remote_ip)

    def get_retry_after(self, remote_ip: str) -> int:
        """Get Retry-After seconds for a rate-limited IP."""
        return self.rate_limiter.retry_after(remote_ip)
