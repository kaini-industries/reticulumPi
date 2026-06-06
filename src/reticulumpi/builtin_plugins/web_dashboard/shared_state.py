"""Shared mutable state for web dashboard modules.

Centralises state that was previously duplicated across api.py and
websocket_handler.py, ensuring both code paths share a single rate limiter.
"""

from __future__ import annotations

import time


class OffgridRateLimiter:
    """Rate limiter for the off-grid mode toggle."""

    RATE_LIMIT: float = 2.0

    def __init__(self) -> None:
        self._last_toggle: float = 0.0

    def check_and_record(self) -> bool:
        """Return True if the toggle is allowed, and record the timestamp.

        Returns False if the last toggle was less than RATE_LIMIT seconds ago.
        """
        now = time.monotonic()
        if now - self._last_toggle < self.RATE_LIMIT:
            return False
        self._last_toggle = now
        return True


offgrid_rate_limiter = OffgridRateLimiter()
