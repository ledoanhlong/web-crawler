"""Tests for app.utils.rate_limiter — AdaptiveRateLimiter."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.utils.rate_limiter import AdaptiveRateLimiter


@pytest.fixture()
def limiter():
    return AdaptiveRateLimiter(min_delay_ms=100, max_delay_ms=5_000)


class TestAdaptiveRateLimiter:
    def test_initial_delay_is_min(self, limiter: AdaptiveRateLimiter):
        assert limiter.current_delay_ms("https://example.com/page") == 100.0

    @pytest.mark.asyncio
    async def test_acquire_enforces_delay(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/a"
        await limiter.acquire(url)
        t0 = time.monotonic()
        await limiter.acquire(url)
        elapsed = time.monotonic() - t0
        # Should have waited at least min_delay (100ms), allow some slack
        assert elapsed >= 0.08

    def test_report_throttle_doubles_delay(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/b"
        assert limiter.current_delay_ms(url) == 100.0
        limiter.report_throttle(url)
        assert limiter.current_delay_ms(url) == 200.0
        limiter.report_throttle(url)
        assert limiter.current_delay_ms(url) == 400.0

    def test_report_throttle_with_retry_after(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/c"
        limiter.report_throttle(url, retry_after=2.0)
        assert limiter.current_delay_ms(url) == 2_000.0

    def test_report_throttle_capped_at_max(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/d"
        for _ in range(20):
            limiter.report_throttle(url)
        assert limiter.current_delay_ms(url) == 5_000.0

    def test_report_success_decreases_after_3(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/e"
        # First increase delay so we can see it decrease
        limiter.report_throttle(url)
        limiter.report_throttle(url)
        high = limiter.current_delay_ms(url)
        # 3 successes should decrease
        limiter.report_success(url)
        limiter.report_success(url)
        limiter.report_success(url)
        assert limiter.current_delay_ms(url) < high

    def test_report_success_floors_at_min(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/f"
        # Already at min, 3 successes should not go below
        for _ in range(10):
            limiter.report_success(url)
        assert limiter.current_delay_ms(url) == 100.0

    def test_reset_single_domain(self, limiter: AdaptiveRateLimiter):
        url = "https://example.com/g"
        limiter.report_throttle(url)
        assert limiter.current_delay_ms(url) > 100.0
        limiter.reset(url)
        assert limiter.current_delay_ms(url) == 100.0

    def test_reset_all(self, limiter: AdaptiveRateLimiter):
        limiter.report_throttle("https://a.com/1")
        limiter.report_throttle("https://b.com/2")
        limiter.reset()
        assert limiter.current_delay_ms("https://a.com/1") == 100.0
        assert limiter.current_delay_ms("https://b.com/2") == 100.0

    def test_different_domains_independent(self, limiter: AdaptiveRateLimiter):
        limiter.report_throttle("https://a.com/x")
        assert limiter.current_delay_ms("https://a.com/x") == 200.0
        assert limiter.current_delay_ms("https://b.com/x") == 100.0

    @pytest.mark.asyncio
    async def test_concurrent_acquire_serialised(self, limiter: AdaptiveRateLimiter):
        """Multiple concurrent acquires for the same domain should be serialised."""
        url = "https://example.com/conc"
        results: list[float] = []

        async def _req():
            await limiter.acquire(url)
            results.append(time.monotonic())

        await asyncio.gather(_req(), _req(), _req())
        # Each should be at least min_delay apart
        for i in range(1, len(results)):
            assert results[i] - results[i - 1] >= 0.08
