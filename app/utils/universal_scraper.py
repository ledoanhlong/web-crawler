"""Thin async wrapper around the universal-scraper library.

Provides helper functions for AI-powered structured extraction using
universal-scraper's auto-generated BeautifulSoup code with intelligent
caching.  All functions return ``None`` on failure so that callers can
fall back gracefully to the existing CSS / SmartScraper pipeline.

Since universal-scraper is synchronous (uses Selenium internally), all
calls are wrapped with ``asyncio.to_thread()`` to avoid blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_scraper: Any = None  # UniversalScraper instance (lazy)
_scraper_lock = threading.Lock()


def _get_scraper(model_name: str | None = None) -> Any:
    """Lazily initialise and return the universal-scraper instance."""
    global _scraper
    if _scraper is not None:
        return _scraper

    with _scraper_lock:
        # Double-check after acquiring lock
        if _scraper is not None:
            return _scraper

        from universal_scraper import UniversalScraper  # import only when needed

        model = model_name or settings.universal_scraper_model
        _scraper = UniversalScraper(model_name=model)
        log.info("universal-scraper initialised (model=%s)", model)
        return _scraper


# ---------------------------------------------------------------------------
# Extract items from a single URL
# ---------------------------------------------------------------------------
async def universal_scraper_extract(
    url: str,
    *,
    fields: list[str],
    model_name: str | None = None,
) -> list[dict] | None:
    """Extract structured data from a URL using universal-scraper.

    Wraps the synchronous ``scrape_url()`` call in ``asyncio.to_thread()``.
    Returns a list of extracted item dicts, or ``None`` on failure.
    """
    def _extract() -> Any:
        scraper = _get_scraper(model_name)
        scraper.set_fields(fields)
        return scraper.scrape_url(url)

    log.info("universal-scraper extract starting for %s (fields: %s)", url, ", ".join(fields))
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_extract),
            timeout=120,
        )
    except asyncio.TimeoutError:
        log.warning("universal-scraper extract timed out for %s after 120s", url)
        return None
    except Exception as exc:
        log.warning("universal-scraper extract failed for %s: %s", url, exc)
        return None

    if result is None:
        return None

    # Normalize result into list[dict]
    items: list[dict] = []
    if isinstance(result, list):
        items = [r for r in result if isinstance(r, dict)]
    elif isinstance(result, dict):
        # Check for common wrapper keys
        for key in ("items", "data", "results", "records"):
            if key in result and isinstance(result[key], list):
                items = [r for r in result[key] if isinstance(r, dict)]
                break
        if not items:
            items = [result]

    log.info("universal-scraper extract OK for %s: %d items", url, len(items))
    return items if items else None


# ---------------------------------------------------------------------------
# Batch extract from multiple URLs
# ---------------------------------------------------------------------------
async def universal_scraper_extract_batch(
    urls: list[str],
    *,
    fields: list[str],
) -> dict[str, list[dict] | None]:
    """Extract from multiple URLs concurrently via ``universal_scraper_extract``.

    Returns ``{url: items_or_None}`` mapping.  Uses a semaphore to limit
    concurrency (Selenium sessions are resource-heavy).
    """
    sem = asyncio.Semaphore(min(settings.max_concurrent_requests, 3))

    async def _extract_one(u: str) -> tuple[str, list[dict] | None]:
        async with sem:
            items = await universal_scraper_extract(u, fields=fields)
            return u, items

    results = await asyncio.gather(*[_extract_one(u) for u in urls], return_exceptions=True)

    out: dict[str, list[dict] | None] = {}
    for i, item in enumerate(results):
        if isinstance(item, Exception):
            failed_url = urls[i] if i < len(urls) else "unknown"
            log.warning("universal-scraper batch error for %s: %s", failed_url, item)
            continue
        url, items = item
        out[url] = items
    return out


# ---------------------------------------------------------------------------
# Detail page extraction
# ---------------------------------------------------------------------------
async def universal_scraper_extract_detail(
    url: str,
    *,
    fields: list[str],
) -> dict[str, str]:
    """Extract fields from a single detail page using universal-scraper.

    Returns a dict of field_name -> value, suitable for enrichment in
    the parser pipeline.  Returns empty dict on failure.
    """
    items = await universal_scraper_extract(url, fields=fields)
    if not items:
        return {}

    # Merge all items into a single flat dict (detail pages have one entity)
    detail: dict[str, str] = {}
    for item in items:
        for k, v in item.items():
            if v is not None and str(v).strip():
                detail[k] = str(v) if not isinstance(v, str) else v

    log.info("universal-scraper detail extraction: %d fields from %s", len(detail), url)
    return detail
