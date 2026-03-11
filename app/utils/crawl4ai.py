"""Thin async wrapper around the Crawl4AI library.

Provides helper functions for fetching pages (with clean markdown output)
and LLM-powered extraction via Crawl4AI's built-in strategies.  All
functions return ``None`` on failure so that callers can fall back
gracefully to the existing Playwright / httpx pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_crawler: Any = None  # AsyncWebCrawler instance (lazy)
_crawler_lock = asyncio.Lock()


async def _get_crawler() -> Any:
    """Lazily initialise and return the async Crawl4AI crawler."""
    global _crawler
    if _crawler is not None:
        return _crawler

    async with _crawler_lock:
        # Double-check after acquiring lock
        if _crawler is not None:
            return _crawler

        from crawl4ai import AsyncWebCrawler, BrowserConfig  # import only when needed

        browser_cfg = BrowserConfig(
            headless=settings.crawl4ai_browser_headless,
            verbose=False,
            extra_args=["--disable-quic"],  # avoid ERR_QUIC_PROTOCOL_ERROR
        )
        _crawler = AsyncWebCrawler(config=browser_cfg)
        await _crawler.start()
        log.info("Crawl4AI async crawler initialised (headless=%s)", settings.crawl4ai_browser_headless)
        return _crawler


# ---------------------------------------------------------------------------
# Fetch a single URL
# ---------------------------------------------------------------------------
async def crawl4ai_fetch(
    url: str,
    *,
    wait_for: str | None = None,
) -> dict | None:
    """Fetch a single URL via Crawl4AI and return clean markdown + HTML.

    Returns a dict with keys ``markdown``, ``html``, ``metadata``,
    or ``None`` if the call fails.
    """
    try:
        from crawl4ai import CrawlerRunConfig  # import only when needed

        run_cfg = CrawlerRunConfig(
            wait_until="networkidle",
            remove_overlay_elements=True,
            excluded_tags=["nav", "footer", "header", "noscript"],
            excluded_selector="#CybotCookiebotDialog, .cookie-banner, [role='banner'], [role='navigation'], [role='contentinfo']",
        )
        if wait_for:
            run_cfg.wait_for = wait_for

        crawler = await _get_crawler()
        result = await crawler.arun(url=url, config=run_cfg)

        if not result.success:
            log.warning("Crawl4AI fetch failed for %s: %s", url, getattr(result, "error_message", "unknown error"))
            return None

        doc: dict[str, Any] = {
            "markdown": result.markdown or "",
            "html": result.html or "",
        }
        if hasattr(result, "metadata") and result.metadata:
            doc["metadata"] = result.metadata
        if hasattr(result, "links") and result.links:
            doc["links"] = result.links

        log.debug("Crawl4AI fetch OK: %s (%d chars markdown)", url, len(doc["markdown"]))
        return doc
    except Exception as exc:
        log.warning("Crawl4AI fetch failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Batch-fetch multiple URLs
# ---------------------------------------------------------------------------
async def crawl4ai_fetch_batch(
    urls: list[str],
) -> dict[str, dict | None]:
    """Fetch multiple URLs concurrently via ``crawl4ai_fetch``.

    Returns ``{url: doc_dict_or_None}`` mapping.  Uses a semaphore to
    limit concurrency.
    """
    sem = asyncio.Semaphore(settings.max_concurrent_requests)

    async def _fetch_one(u: str) -> tuple[str, dict | None]:
        async with sem:
            doc = await crawl4ai_fetch(u)
            return u, doc

    results = await asyncio.gather(*[_fetch_one(u) for u in urls], return_exceptions=True)

    out: dict[str, dict | None] = {}
    for i, item in enumerate(results):
        if isinstance(item, Exception):
            failed_url = urls[i] if i < len(urls) else "unknown"
            log.warning("Crawl4AI batch fetch error for %s: %s", failed_url, item)
            continue
        url, doc = item
        out[url] = doc
    return out


# ---------------------------------------------------------------------------
# LLM-powered structured extraction
# ---------------------------------------------------------------------------
async def crawl4ai_extract(
    url: str,
    *,
    fields: list[str],
    provider: str | None = None,
    api_token: str | None = None,
) -> list[dict] | None:
    """Extract structured data from a URL using Crawl4AI's LLM extraction.

    Uses Crawl4AI's ``LLMExtractionStrategy`` to extract fields from the
    page.  Returns a list of extracted item dicts, or ``None`` on failure.
    """
    try:
        from crawl4ai import CrawlerRunConfig  # import only when needed
        from crawl4ai.extraction_strategy import LLMExtractionStrategy

        extraction = LLMExtractionStrategy(
            provider=provider or f"azure/{settings.azure_openai_deployment}",
            api_token=api_token or settings.azure_openai_api_key,
            instruction=(
                f"Extract the following fields from the page content: {', '.join(fields)}. "
                "Return a JSON array of objects, each object containing these fields."
            ),
        )

        run_cfg = CrawlerRunConfig(
            extraction_strategy=extraction,
            wait_until="networkidle",
            remove_overlay_elements=True,
            excluded_tags=["nav", "footer", "header", "noscript"],
            excluded_selector="#CybotCookiebotDialog, .cookie-banner, [role='banner'], [role='navigation'], [role='contentinfo']",
        )

        crawler = await _get_crawler()
        result = await crawler.arun(url=url, config=run_cfg)

        if not result.success:
            log.warning("Crawl4AI extract failed for %s: %s", url, getattr(result, "error_message", "unknown error"))
            return None

        import json

        extracted = result.extracted_content
        if isinstance(extracted, str):
            extracted = json.loads(extracted)
        if isinstance(extracted, list):
            log.info("Crawl4AI extract OK for %s: %d items", url, len(extracted))
            return extracted
        if isinstance(extracted, dict):
            return [extracted]
        return None
    except Exception as exc:
        log.warning("Crawl4AI extract failed for %s: %s", url, exc)
        return None
