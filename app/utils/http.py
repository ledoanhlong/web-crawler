"""Shared async HTTP client with retry, backoff, and response intelligence."""

from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.utils.logging import get_logger
from app.utils.rate_limiter import rate_limiter

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Shared httpx client (lazy init, connection pooling)
# ---------------------------------------------------------------------------
_shared_client: httpx.AsyncClient | None = None


def _get_shared_client() -> httpx.AsyncClient:
    """Return a shared AsyncClient, creating it on first use."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=settings.request_timeout_s,
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
            ),
        )
    return _shared_client


async def close_shared_client() -> None:
    """Close the shared HTTP client. Call on application shutdown."""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None

# ---------------------------------------------------------------------------
# User-Agent pool (rotated per-request for stealth)
# ---------------------------------------------------------------------------
_USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]


def _rotate_user_agent() -> str:
    """Pick a random UA from the pool."""
    return random.choice(_USER_AGENTS)


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _rotate_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


# ---------------------------------------------------------------------------
# FetchResult — rich response wrapper
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    """Wraps an HTTP response with useful metadata."""

    text: str = ""
    status_code: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    response_time_ms: float = 0.0

    # Parsed convenience properties ----------------------------------------
    @property
    def content_type(self) -> str:
        ct = self.headers.get("content-type", "")
        return ct.split(";")[0].strip().lower()

    @property
    def is_json(self) -> bool:
        return "json" in self.content_type

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def retry_after(self) -> float | None:
        """Parse Retry-After header (seconds)."""
        val = self.headers.get("retry-after")
        if val is None:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    @property
    def rate_limit_remaining(self) -> int | None:
        for key in ("x-ratelimit-remaining", "x-rate-limit-remaining"):
            val = self.headers.get(key)
            if val is not None:
                try:
                    return int(val)
                except ValueError:
                    pass
        return None


def parse_link_header(header: str) -> dict[str, str]:
    """Parse an HTTP Link header (RFC 5988) into {rel: url} mapping.

    Example: ``<https://api.example.com/items?page=2>; rel="next"``
    → ``{"next": "https://api.example.com/items?page=2"}``
    """
    links: dict[str, str] = {}
    for part in header.split(","):
        match = re.match(r'\s*<([^>]+)>\s*;\s*rel="([^"]+)"', part.strip())
        if match:
            links[match.group(2)] = match.group(1)
    return links


# ---------------------------------------------------------------------------
# Core fetch with retry + exponential backoff
# ---------------------------------------------------------------------------
async def fetch_page_full(
    url: str,
    *,
    follow_redirects: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch a URL with retry / backoff and return a rich FetchResult.

    Retries on transient errors (429, 5xx, timeouts) up to
    ``settings.http_max_retries`` times with exponential backoff.
    Respects the ``Retry-After`` header on 429 responses.
    """
    headers = _default_headers()
    if extra_headers:
        headers.update(extra_headers)

    last_exc: Exception | None = None
    max_attempts = 1 + settings.http_max_retries

    for attempt in range(max_attempts):
        # Respect per-domain rate limit before each attempt
        await rate_limiter.acquire(url)
        t0 = time.monotonic()
        try:
            client = _get_shared_client()
            resp = await client.get(url, headers=headers)
            elapsed = (time.monotonic() - t0) * 1000

            result = FetchResult(
                text=resp.text,
                status_code=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                response_time_ms=elapsed,
            )

            # Success
            if resp.status_code < 400:
                rate_limiter.report_success(url)
                if attempt > 0:
                    log.info("HTTP GET %s succeeded on attempt %d", url, attempt + 1)
                else:
                    log.info("HTTP GET %s → %d (%.0f ms)", url, resp.status_code, elapsed)
                return result

            # Retryable status code?
            if resp.status_code in settings.http_retry_status_codes and attempt < max_attempts - 1:
                rate_limiter.report_throttle(url, result.retry_after)
                delay = _compute_backoff(attempt, result.retry_after)
                log.warning(
                    "HTTP GET %s → %d — retrying in %.1fs (attempt %d/%d)",
                    url, resp.status_code, delay, attempt + 1, max_attempts,
                )
                await asyncio.sleep(delay)
                continue

            # Non-retryable error or last attempt
            resp.raise_for_status()

        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            elapsed = (time.monotonic() - t0) * 1000
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = _compute_backoff(attempt)
                log.warning(
                    "HTTP GET %s failed (%s) — retrying in %.1fs (attempt %d/%d)",
                    url, type(exc).__name__, delay, attempt + 1, max_attempts,
                )
                await asyncio.sleep(delay)
                continue
            raise

    # Should not reach here, but just in case
    if last_exc:
        raise last_exc
    return FetchResult()


def _compute_backoff(attempt: int, retry_after: float | None = None) -> float:
    """Exponential backoff with jitter and Retry-After respect."""
    base = settings.http_backoff_factor * (2 ** attempt)
    jitter = random.uniform(0, base * 0.25)
    delay = base + jitter
    if retry_after is not None and retry_after > delay:
        delay = retry_after
    return min(delay, settings.max_request_delay_ms / 1000)


# ---------------------------------------------------------------------------
# Backward-compatible wrappers
# ---------------------------------------------------------------------------
async def fetch_page(url: str, *, follow_redirects: bool = True) -> str:
    """Fetch a URL with httpx and return the response text.

    Thin wrapper around :func:`fetch_page_full` for backward compatibility.
    """
    result = await fetch_page_full(url, follow_redirects=follow_redirects)
    if result.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"HTTP {result.status_code}",
            request=httpx.Request("GET", url),
            response=httpx.Response(result.status_code),
        )
    return result.text


async def fetch_pages(urls: list[str]) -> dict[str, str]:
    """Fetch multiple URLs concurrently (respecting concurrency limit)."""
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    results: dict[str, str] = {}

    async def _fetch(url: str) -> None:
        async with semaphore:
            try:
                results[url] = await fetch_page(url)
            except Exception as exc:
                log.warning("Failed to fetch %s: %s", url, exc)
                results[url] = ""

    await asyncio.gather(*[_fetch(u) for u in urls])
    return results
