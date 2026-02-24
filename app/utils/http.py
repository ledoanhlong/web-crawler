"""Shared async HTTP client with sensible defaults."""

from __future__ import annotations

import asyncio

import httpx

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _default_headers() -> dict[str, str]:
    return {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


async def fetch_page(url: str, *, follow_redirects: bool = True) -> str:
    """Fetch a URL with httpx and return the response text."""
    async with httpx.AsyncClient(
        headers=_default_headers(),
        timeout=settings.request_timeout_s,
        follow_redirects=follow_redirects,
    ) as client:
        log.info("HTTP GET %s", url)
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def fetch_pages(urls: list[str]) -> dict[str, str]:
    """Fetch multiple URLs concurrently (respecting concurrency limit)."""
    semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
    results: dict[str, str] = {}

    async def _fetch(url: str) -> None:
        async with semaphore:
            await asyncio.sleep(settings.request_delay_ms / 1000)
            try:
                results[url] = await fetch_page(url)
            except Exception as exc:
                log.warning("Failed to fetch %s: %s", url, exc)
                results[url] = ""

    await asyncio.gather(*[_fetch(u) for u in urls])
    return results
