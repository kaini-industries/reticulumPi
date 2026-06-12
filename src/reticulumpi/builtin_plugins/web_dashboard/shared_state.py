"""Shared mutable state for web dashboard modules.

Centralises state that was previously duplicated across api.py and
websocket_handler.py, ensuring both code paths share a single rate limiter.
"""

from __future__ import annotations

import time

from reticulumpi.builtin_plugins.web_dashboard.auth import RateLimiter


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

# Per-IP sliding-window limiter for the client-error reporting endpoint
# (POST /api/client_error). Bounds log-flood / feedback-loop risk from the
# browser-side error reporter (errlog.js). Distinct from the global offgrid
# debounce above — this is keyed per remote IP.
client_error_rate_limiter = RateLimiter(max_attempts=20, window_seconds=60)
