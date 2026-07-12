"""Tests for the web dashboard auth module."""

import hashlib
import os

from reticulumpi.builtin_plugins.web_dashboard.auth import verify_password


# --- gap-010: verify_password with v1-format hash ---


class TestVerifyPasswordV1Format:
    """Test verify_password against manually crafted v1-format hashes.

    The v1 format is: ``scrypt:<salt_hex>:<hash_hex>``
    with fixed params n=16384 (2^14), r=8, p=1, dklen=32.
    """

    def _make_v1_hash(self, password: str, salt: bytes) -> str:
        """Build a v1 scrypt hash string from a known password and salt."""
        dk = hashlib.scrypt(
            password.encode(),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=32,
        )
        return f"scrypt:{salt.hex()}:{dk.hex()}"

    def test_correct_password_returns_true(self):
        salt = os.urandom(16)
        password = "correct-horse-battery-staple"
        stored_hash = self._make_v1_hash(password, salt)
        assert verify_password(password, stored_hash) is True

    def test_wrong_password_returns_false(self):
        salt = os.urandom(16)
        password = "correct-horse-battery-staple"
        stored_hash = self._make_v1_hash(password, salt)
        assert verify_password("wrong-password", stored_hash) is False

    def test_empty_password_returns_false(self):
        salt = os.urandom(16)
        password = "some-secret"
        stored_hash = self._make_v1_hash(password, salt)
        assert verify_password("", stored_hash) is False

    def test_v1_hash_with_known_salt(self):
        """Deterministic test with a fixed salt for reproducibility."""
        salt = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10"
        password = "test123"
        # Compute expected hash
        dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        stored_hash = f"scrypt:{salt.hex()}:{dk.hex()}"
        assert verify_password(password, stored_hash) is True
        assert verify_password("test124", stored_hash) is False

    def test_malformed_hash_returns_false(self):
        assert verify_password("anything", "not-a-valid-hash") is False

    def test_wrong_prefix_returns_false(self):
        assert verify_password("anything", "bcrypt:aabbcc:ddeeff") is False

    def test_empty_hash_returns_false(self):
        assert verify_password("anything", "") is False
