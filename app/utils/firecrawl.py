"""Thin async wrapper around the FireCrawl Python SDK.

Provides helper functions for scraping, URL discovery, batch scraping,
and LLM-powered extraction via the FireCrawl Cloud API.  All functions
return ``None`` on failure so that callers can fall back gracefully to
the existing Playwright / httpx pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_client: Any = None  # AsyncFirecrawl instance (lazy)


def _get_client() -> Any:
    """Lazily initialise and return the async FireCrawl client."""
    global _client
    if _client is not None:
        return _client

    if not settings.firecrawl_api_key:
        raise RuntimeError(
            "FireCrawl API key is not configured. "
            "Set FIRECRAWL_API_KEY in your .env file."
        )

    from firecrawl import AsyncFirecrawl  # import only when needed

    _client = AsyncFirecrawl(
        api_key=settings.firecrawl_api_key,
        api_url=settings.firecrawl_api_url,
    )
    log.info("FireCrawl async client initialised (endpoint=%s)", settings.firecrawl_api_url)
    return _client


# ---------------------------------------------------------------------------
# Scrape a single URL
# ---------------------------------------------------------------------------
async def firecrawl_scrape(
    url: str,
    *,
    formats: list[str] | None = None,
    actions: list[dict] | None = None,
    wait_for: int | None = None,
    only_main_content: bool = True,
) -> dict | None:
    """Scrape a single URL via FireCrawl ``/v2/scrape``.

    Returns a dict with keys like ``markdown``, ``html``, ``metadata``,
    ``links``, or ``None`` if the call fails.
    """
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {}
        if formats:
            kwargs["formats"] = formats
        if actions:
            kwargs["actions"] = actions
        if wait_for is not None:
            kwargs["wait_for"] = wait_for
        kwargs["only_main_content"] = only_main_content

        result = await client.scrape(url, **kwargs)

        # The SDK returns a Document object — convert to dict for easy access
        doc: dict[str, Any] = {}
        if hasattr(result, "markdown"):
            doc["markdown"] = result.markdown
        if hasattr(result, "html"):
            doc["html"] = result.html
        if hasattr(result, "raw_html"):
            doc["raw_html"] = result.raw_html
        if hasattr(result, "metadata"):
            doc["metadata"] = (
                result.metadata.model_dump()
                if hasattr(result.metadata, "model_dump")
                else result.metadata
            )
        if hasattr(result, "links"):
            doc["links"] = result.links
        log.debug("FireCrawl scrape OK: %s (%d chars markdown)", url, len(doc.get("markdown", "") or ""))
        return doc
    except Exception as exc:
        log.warning("FireCrawl scrape failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Discover URLs on a site
# ---------------------------------------------------------------------------
async def firecrawl_map(
    url: str,
    *,
    search: str | None = None,
    limit: int | None = None,
    include_subdomains: bool = False,
) -> list[dict[str, str]] | None:
    """Discover all URLs on a website via FireCrawl ``/v2/map``.

    Returns a list of ``{url, title, description}`` dicts, or ``None``
    on failure.
    """
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {}
        if search:
            kwargs["search"] = search
        if limit is not None:
            kwargs["limit"] = limit
        kwargs["include_subdomains"] = include_subdomains

        result = await client.map(url, **kwargs)

        links: list[dict[str, str]] = []
        for item in (result.links or []):
            entry: dict[str, str] = {"url": item.url}
            if hasattr(item, "title") and item.title:
                entry["title"] = item.title
            if hasattr(item, "description") and item.description:
                entry["description"] = item.description
            links.append(entry)

        log.info("FireCrawl map discovered %d URLs for %s", len(links), url)
        return links
    except Exception as exc:
        log.warning("FireCrawl map failed for %s: %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# Batch-scrape multiple URLs
# ---------------------------------------------------------------------------
async def firecrawl_scrape_batch(
    urls: list[str],
    *,
    formats: list[str] | None = None,
    only_main_content: bool = True,
) -> dict[str, dict | None]:
    """Scrape multiple URLs concurrently via ``firecrawl_scrape``.

    Returns ``{url: doc_dict_or_None}`` mapping.  Uses a semaphore to
    limit concurrency and avoid overwhelming the API.
    """
    sem = asyncio.Semaphore(settings.max_concurrent_requests)

    async def _scrape_one(u: str) -> tuple[str, dict | None]:
        async with sem:
            doc = await firecrawl_scrape(
                u, formats=formats, only_main_content=only_main_content,
            )
            return u, doc

    results = await asyncio.gather(*[_scrape_one(u) for u in urls], return_exceptions=True)

    out: dict[str, dict | None] = {}
    for i, item in enumerate(results):
        if isinstance(item, Exception):
            failed_url = urls[i] if i < len(urls) else "unknown"
            log.warning("FireCrawl batch scrape error for %s: %s", failed_url, item)
            continue
        url, doc = item
        out[url] = doc
    return out


# ---------------------------------------------------------------------------
# LLM-powered structured extraction
# ---------------------------------------------------------------------------
async def firecrawl_extract(
    urls: list[str],
    *,
    prompt: str,
    schema: dict | None = None,
) -> dict | None:
    """Extract structured data via FireCrawl ``agent()`` endpoint.

    Returns the extracted data dict, or ``None`` on failure.
    """
    try:
        client = _get_client()
        kwargs: dict[str, Any] = {
            "prompt": prompt,
        }
        if schema:
            kwargs["schema"] = schema

        result = await client.agent(urls, **kwargs)

        # The SDK returns an AgentResponse — extract the data
        if hasattr(result, "data"):
            data = result.data
            log.info("FireCrawl extract OK for %d URL(s)", len(urls))
            return data
        return None
    except Exception as exc:
        log.warning("FireCrawl extract failed for %d URL(s): %s", len(urls), exc)
        return None
