"""Rate limiter for Telegram API flood control handling.

This helper tracks RetryAfter backoff windows and exposes whether
Telegram calls should be deferred to avoid flood control throttling.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from loguru import logger


class RateLimiter:
    """Handles Telegram API rate limiting and flood control backoff."""

    def __init__(self) -> None:
        self._retry_until: Optional[datetime] = None

    @property
    def retry_until(self) -> Optional[datetime]:
        """Get the timestamp when backoff expires."""
        return self._retry_until

    def is_in_backoff(self) -> bool:
        """Check if currently in backoff period."""
        if self._retry_until is None:
            return False
        return datetime.now(timezone.utc) < self._retry_until

    def get_backoff_remaining(self) -> float:
        """Get remaining backoff time in seconds."""
        if not self.is_in_backoff():
            return 0.0
        remaining = (self._retry_until - datetime.now(timezone.utc)).total_seconds()  # type: ignore[operator]
        return max(0.0, remaining)

    def apply_backoff(self, retry_seconds: int) -> None:
        """Apply backoff period after hitting rate limit."""
        self._retry_until = datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
        logger.warning(f"Telegram flood control: backing off for {retry_seconds} seconds")

    def clear_backoff(self) -> None:
        """Clear backoff period."""
        self._retry_until = None
