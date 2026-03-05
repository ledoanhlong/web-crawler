"""Adaptive rate limiter — adjusts request delay based on server signals.

Uses a token-bucket-style approach with exponential back-off when the server
returns 429 or 5xx, and gradual speed-up when requests succeed.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)


class AdaptiveRateLimiter:
    """Per-domain rate limiter that adapts to server responses.

    Parameters
    ----------
    min_delay_ms : int
        Minimum delay between requests (floor).
    max_delay_ms : int
        Maximum delay between requests (ceiling).
    """

    def __init__(
        self,
        min_delay_ms: int | None = None,
        max_delay_ms: int | None = None,
    ) -> None:
        self._min_delay = (min_delay_ms or settings.min_request_delay_ms) / 1000
        self._max_delay = (max_delay_ms or settings.max_request_delay_ms) / 1000
        # Per-domain state
        self._current_delay: dict[str, float] = defaultdict(lambda: self._min_delay)
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._consecutive_ok: dict[str, int] = defaultdict(int)

    @staticmethod
    def _domain(url: str) -> str:
        """Extract domain from URL for per-domain tracking."""
        from urllib.parse import urlparse

        return urlparse(url).netloc or url

    async def acquire(self, url: str) -> None:
        """Wait until it is safe to make a request to *url*'s domain."""
        domain = self._domain(url)
        async with self._locks[domain]:
            now = time.monotonic()
            elapsed = now - self._last_request[domain]
            delay = self._current_delay[domain]
            if elapsed < delay:
                wait = delay - elapsed
                log.debug("Rate limiter: sleeping %.2fs for %s", wait, domain)
                await asyncio.sleep(wait)
            self._last_request[domain] = time.monotonic()

    def report_success(self, url: str) -> None:
        """Report a successful request — gradually decrease delay."""
        domain = self._domain(url)
        self._consecutive_ok[domain] += 1
        # Speed up after 3 consecutive successes
        if self._consecutive_ok[domain] >= 3:
            new_delay = max(self._min_delay, self._current_delay[domain] * 0.8)
            if new_delay != self._current_delay[domain]:
                log.debug("Rate limiter: decreasing delay to %.2fs for %s", new_delay, domain)
                self._current_delay[domain] = new_delay
            self._consecutive_ok[domain] = 0

    def report_throttle(self, url: str, retry_after: float | None = None) -> None:
        """Report a 429 / server error — exponentially increase delay."""
        domain = self._domain(url)
        self._consecutive_ok[domain] = 0
        if retry_after and retry_after > 0:
            new_delay = min(self._max_delay, retry_after)
        else:
            new_delay = min(self._max_delay, self._current_delay[domain] * 2)
        log.info("Rate limiter: increasing delay to %.2fs for %s", new_delay, domain)
        self._current_delay[domain] = new_delay

    def current_delay_ms(self, url: str) -> float:
        """Return current delay in milliseconds for *url*'s domain."""
        return self._current_delay[self._domain(url)] * 1000

    def reset(self, url: str | None = None) -> None:
        """Reset state for a domain, or all domains if *url* is ``None``."""
        if url:
            domain = self._domain(url)
            self._current_delay.pop(domain, None)
            self._last_request.pop(domain, None)
            self._consecutive_ok.pop(domain, None)
        else:
            self._current_delay.clear()
            self._last_request.clear()
            self._consecutive_ok.clear()


# Module-level singleton
rate_limiter = AdaptiveRateLimiter()
