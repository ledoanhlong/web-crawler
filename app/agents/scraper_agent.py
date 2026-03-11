"""ScraperAgent — Executes a ScrapingPlan and returns raw PageData.

Supports:
- Static pages via httpx (or Scrapy when enabled)
- JS-rendered pages via Playwright
- Crawl4AI for local async crawling with markdown output
- universal-scraper for AI-powered BS4 extraction with caching
- Multiple pagination strategies (next button, page numbers, alphabet tabs,
  infinite scroll, load-more, and direct API endpoints)
- Optional detail-page enrichment
- Preview mode: scrape a single item (with its detail page) for user validation
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models.schemas import DetailApiPlan, ExtractionMethod, PageData, PaginationStrategy, ScrapingPlan, ScrapingTarget
from app.utils.browser import (
    click_all_tabs,
    click_load_more,
    create_page,
    fetch_page_js,
    get_browser,
    safe_click,
    scroll_to_bottom,
)
from app.utils.http import fetch_page, fetch_pages
from app.utils.logging import get_logger
from app.utils.structured_data import extract_all_structured_data

log = get_logger(__name__)

# Batch size for detail page fetching — checked between batches for cancel
_DETAIL_BATCH_SIZE = 10


@dataclass
class EnrichResult:
    """Result of detail-page enrichment with resume tracking."""
    pages: list[PageData]
    fetched_urls: list[str] = field(default_factory=list)
    remaining_urls: list[str] = field(default_factory=list)


class ScraperAgent:
    """Execute a scraping plan and return raw extracted data."""

    @staticmethod
    def _emit_page_progress(
        progress_callback: Callable[[dict], None] | None,
        *,
        method: ExtractionMethod | None,
        page_url: str,
        page_items: int,
        pages_processed: int,
        total_items: int,
    ) -> None:
        """Emit normalized per-page extraction progress for orchestrator health checks."""
        if not progress_callback:
            return
        progress_callback(
            {
                "stage": "scraping_pages",
                "method": method.value if method else "auto",
                "page_url": page_url,
                "page_items": page_items,
                "pages_processed": pages_processed,
                "total_items": total_items,
            }
        )

    async def scrape(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[PageData], EnrichResult | None]:
        """Scrape pages and return (pages, enrich_result).

        enrich_result is non-None when detail enrichment was performed,
        and contains fetched/remaining URL lists for resume tracking.
        """
        log.info("Starting scrape for %s (js=%s, max_items=%s, method=%s)", plan.url, plan.requires_javascript, max_items, extraction_method)

        enrich_result: EnrichResult | None = None

        # Listing API path — direct JSON fetch, no HTML parsing needed
        if extraction_method == ExtractionMethod.LISTING_API and plan.listing_api_plan:
            pages = await self._scrape_listing_api(
                plan, max_items=max_items,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )
            return pages, enrich_result

        # Crawl4AI path — use when explicitly selected or when enabled for fetching
        _crawl4ai_compatible = plan.pagination in (
            PaginationStrategy.NONE,
            PaginationStrategy.PAGE_NUMBERS,
            PaginationStrategy.NEXT_BUTTON,
        )
        _use_crawl4ai = (
            (extraction_method == ExtractionMethod.CRAWL4AI and _crawl4ai_compatible)
            or (settings.use_crawl4ai and settings.use_crawl4ai_for_fetching and _crawl4ai_compatible)
        )
        if _use_crawl4ai:
            pages, enrich_result = await self._scrape_crawl4ai(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
        elif extraction_method == ExtractionMethod.CRAWL4AI and not _crawl4ai_compatible:
            actual = "js" if plan.requires_javascript else "static"
            log.warning(
                "Crawl4AI selected but pagination '%s' is not compatible — "
                "falling back to %s path",
                plan.pagination.value, actual,
            )
            if progress_callback:
                progress_callback({"stage": "method_fallback", "requested_method": extraction_method.value, "actual_method": actual, "fallback_reason": f"pagination '{plan.pagination.value}' incompatible"})
            if plan.requires_javascript:
                pages, enrich_result = await self._scrape_js(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
            else:
                pages, enrich_result = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)

        # universal-scraper path — use when explicitly selected or when enabled
        elif plan.pagination in (
            PaginationStrategy.NONE,
            PaginationStrategy.PAGE_NUMBERS,
            PaginationStrategy.NEXT_BUTTON,
        ) and (
            (extraction_method == ExtractionMethod.UNIVERSAL_SCRAPER)
            or (settings.use_universal_scraper and settings.use_universal_scraper_for_extraction)
        ):
            pages, enrich_result = await self._scrape_universal_scraper(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
        elif extraction_method == ExtractionMethod.UNIVERSAL_SCRAPER and plan.pagination not in (PaginationStrategy.NONE, PaginationStrategy.PAGE_NUMBERS, PaginationStrategy.NEXT_BUTTON):
            actual = "js" if plan.requires_javascript else "static"
            log.warning(
                "universal-scraper selected but pagination '%s' is not compatible — "
                "falling back to %s path",
                plan.pagination.value, actual,
            )
            if progress_callback:
                progress_callback({"stage": "method_fallback", "requested_method": extraction_method.value, "actual_method": actual, "fallback_reason": f"pagination '{plan.pagination.value}' incompatible"})
            if plan.requires_javascript:
                pages, enrich_result = await self._scrape_js(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
            else:
                pages, enrich_result = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
        elif plan.pagination == PaginationStrategy.API_ENDPOINT and plan.api_endpoint:
            pages = await self._scrape_api(plan, max_items=max_items)
        elif plan.requires_javascript:
            pages, enrich_result = await self._scrape_js(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
        elif settings.use_scrapy and self._can_use_scrapy(plan):
            try:
                pages = await self._scrape_scrapy(plan, max_items=max_items)
                # Spider already followed detail links and captured HTML.
                # Still need sub-links and API enrichment (run in main process).
                if plan.detail_page_plan and plan.detail_page_plan.sub_links:
                    all_detail_htmls: dict[str, str] = {}
                    for pd in pages:
                        all_detail_htmls.update(pd.detail_pages)
                    if all_detail_htmls:
                        sub_link_data = await self._follow_detail_sub_links(
                            all_detail_htmls, plan
                        )
                        for pd in pages:
                            pd.detail_sub_pages.update(sub_link_data)
                pages = await self._enrich_detail_api(pages, plan)
            except Exception as exc:
                log.warning("Scrapy failed, falling back to httpx: %s", exc)
                pages, enrich_result = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)
        else:
            pages, enrich_result = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback, cancel_event=cancel_event)

        # Detail page enrichment (shared for all paths that haven't done it yet)
        # Note: _scrape_js and _scrape_static already call _enrich_detail_pages internally,
        # so this is only a fallback for API/scrapy paths.

        # Enforce max_items limit across all pages
        if max_items is not None:
            total = 0
            for pd in pages:
                remaining = max_items - total
                if remaining <= 0:
                    pd.items = []
                elif len(pd.items) > remaining:
                    pd.items = pd.items[:remaining]
                total += len(pd.items)
            log.info("max_items=%d: kept %d items total", max_items, total)

        # Cross-page deduplication
        seen_keys: set[str] = set()
        total_before = sum(len(pd.items) for pd in pages)
        for pd in pages:
            unique_items: list[dict[str, str | None]] = []
            for item in pd.items:
                dedup_key = item.get("detail_link") or ""
                if not dedup_key:
                    name = (item.get("name") or "").strip().lower()
                    vals = "|".join(
                        (v or "").strip().lower()
                        for k, v in sorted(item.items())
                        if k != "detail_link" and v
                    )
                    dedup_key = f"{name}||{vals}"
                if dedup_key and dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    unique_items.append(item)
            pd.items = unique_items
        total_after = sum(len(pd.items) for pd in pages)
        if total_before != total_after:
            log.info(
                "Deduplication: %d -> %d items (%d duplicates removed)",
                total_before, total_after, total_before - total_after,
            )

        return pages, enrich_result

    async def scrape_preview(self, plan: ScrapingPlan) -> list[PageData]:
        """Scrape just the first item (with its detail page) for preview."""
        log.info("Preview scrape for %s", plan.url)

        if plan.pagination == PaginationStrategy.API_ENDPOINT and plan.api_endpoint:
            pages = await self._scrape_api(plan, max_items=1)
        elif plan.requires_javascript:
            pages = await self._scrape_preview_js(plan)
        elif settings.use_scrapy and self._can_use_scrapy(plan):
            try:
                pages = await self._scrape_scrapy(plan, max_items=1)
            except Exception as exc:
                log.warning("Scrapy preview failed, falling back to httpx: %s", exc)
                pages = await self._scrape_preview_static(plan)
        else:
            pages = await self._scrape_preview_static(plan)

        # Enrich the single preview item with its detail page / API
        if pages and pages[0].items:
            pages = (await self._enrich_detail_pages(pages, plan, max_details=1)).pages
            pages = await self._enrich_detail_api(pages, plan, max_details=1)

        return pages

    async def scrape_preview_dual(self, plan: ScrapingPlan) -> tuple[list[PageData], list[PageData], list[PageData], list[PageData], list[PageData]]:
        """Fetch the *landing page only* and return up to five previews.

        Returns (css_pages, smart_pages, crawl4ai_pages, us_pages, listing_api_pages).
        CSS returns up to 5 items for better validation; others return 1.
        crawl4ai_pages / us_pages / listing_api_pages are populated only when enabled.
        This is intentionally lightweight — no pagination, no tab clicking,
        no scrolling — just the first page load.
        """
        log.info("Dual preview scrape for %s", plan.url)

        # ── Fetch the landing page once (no pagination / tabs) ──────────
        used_js = plan.requires_javascript
        inner_text = ""  # shadow-DOM visible text (populated by Playwright)
        if plan.requires_javascript:
            async with get_browser() as browser:
                result = await fetch_page_js(
                    browser, plan.url, wait_selector=plan.wait_selector,
                    capture_inner_text=True,
                )
                html, inner_text = result  # type: ignore[misc]
        else:
            html = await fetch_page(plan.url)

        if not html:
            return [], [], [], []

        # If CSS found 0 items and we used httpx, retry with Playwright
        preview_html = self._slice_html_for_preview(html, plan.target.item_container_selector)
        css_items = self._extract_items(preview_html, plan.target, plan.detail_api_plan)[:5]

        if not css_items and not used_js:
            log.warning("CSS found 0 items with static fetch — retrying with Playwright for %s", plan.url)
            async with get_browser() as browser:
                result = await fetch_page_js(
                    browser, plan.url, wait_selector=plan.wait_selector,
                    capture_inner_text=True,
                )
                html, inner_text = result  # type: ignore[misc]
            if html:
                preview_html = self._slice_html_for_preview(html, plan.target.item_container_selector)
                css_items = self._extract_items(preview_html, plan.target, plan.detail_api_plan)[:5]

        # ── Run SmartScraperGraph on the sliced HTML ────────────────────
        import time as _time

        smart_items: list[dict[str, str | None]] = []
        if settings.use_smart_scraper_primary and len(preview_html) >= 500:
            from app.utils.smart_scraper import smart_extract_items

            fields = list(plan.target.field_selectors.keys())
            if plan.target.detail_link_selector and "detail_link" not in fields:
                fields.append("detail_link")
            t0 = _time.monotonic()
            log.info("SmartScraper preview starting (%d chars HTML)", len(preview_html))
            smart_items = await smart_extract_items(preview_html, fields, max_items=1)
            log.info("SmartScraper preview finished in %.1fs (%d items)", _time.monotonic() - t0, len(smart_items))
            if smart_items and plan.target.detail_link_selector:
                self._backfill_detail_links(preview_html, smart_items, plan.target)
            smart_items = smart_items[:1]

        # ── Run Crawl4AI extraction if enabled ──────────────────────────
        crawl4ai_items: list[dict[str, str | None]] = []
        _c4_preview_enabled = settings.use_crawl4ai and (
            settings.use_crawl4ai_for_extraction or settings.use_crawl4ai_for_fetching
        )
        if _c4_preview_enabled:
            t0 = _time.monotonic()
            fields = list(plan.target.field_selectors.keys())
            if plan.target.detail_link_selector and "detail_link" not in fields:
                fields.append("detail_link")

            if settings.use_crawl4ai_for_extraction:
                # Use Crawl4AI's own LLM extraction
                from app.utils.crawl4ai import crawl4ai_extract

                log.info("Crawl4AI extract preview starting for %s", plan.url)
                c4_data = await crawl4ai_extract(plan.url, fields=fields)
                if c4_data:
                    crawl4ai_items = c4_data[:1]
            else:
                # Use Crawl4AI to fetch markdown, then extract with LLM
                from app.utils.crawl4ai import crawl4ai_fetch
                from app.utils.smart_scraper import smart_extract_items_from_markdown

                log.info("Crawl4AI fetch preview starting for %s", plan.url)
                c4_doc = await crawl4ai_fetch(plan.url)
                if c4_doc and c4_doc.get("markdown"):
                    crawl4ai_items = await smart_extract_items_from_markdown(
                        c4_doc["markdown"], fields, max_items=1,
                    )
                    if crawl4ai_items and plan.target.detail_link_selector:
                        c4_html = c4_doc.get("html", "")
                        if c4_html:
                            self._backfill_detail_links(c4_html, crawl4ai_items, plan.target)
                    crawl4ai_items = crawl4ai_items[:1]

                # innerText fallback: shadow-DOM content invisible to markdown
                if (
                    not crawl4ai_items
                    and settings.use_inner_text_fallback
                    and inner_text
                    and len(inner_text) > 500
                ):
                    log.info(
                        "Crawl4AI markdown yielded 0 items — trying innerText fallback (%d chars)",
                        len(inner_text),
                    )
                    crawl4ai_items = await smart_extract_items_from_markdown(
                        inner_text, fields, max_items=1,
                    )
                    crawl4ai_items = crawl4ai_items[:1]
            log.info("Crawl4AI preview finished in %.1fs (%d items)", _time.monotonic() - t0, len(crawl4ai_items))

        # ── Run universal-scraper extraction if enabled ──────────────────
        us_items: list[dict[str, str | None]] = []
        _us_preview_enabled = settings.use_universal_scraper and settings.use_universal_scraper_for_extraction
        if _us_preview_enabled:
            t0 = _time.monotonic()
            fields = list(plan.target.field_selectors.keys())
            if plan.target.detail_link_selector and "detail_link" not in fields:
                fields.append("detail_link")

            from app.utils.universal_scraper import universal_scraper_extract

            log.info("universal-scraper preview starting for %s", plan.url)
            us_data = await universal_scraper_extract(plan.url, fields=fields)
            if us_data:
                us_items = us_data[:1]
            log.info("universal-scraper preview finished in %.1fs (%d items)", _time.monotonic() - t0, len(us_items))

        # ── Run listing API interception if enabled ─────────────────────
        listing_api_items: list[dict[str, str | None]] = []
        if settings.use_listing_api_interception:
            from app.utils.browser import intercept_listing_api
            from app.models.schemas import ListingApiPlan

            t0 = _time.monotonic()
            log.info("Listing API interception starting for %s", plan.url)
            try:
                async with get_browser() as browser:
                    api_url, api_items, json_path = await intercept_listing_api(
                        browser, plan.url,
                        wait_selector=plan.wait_selector,
                    )
                if api_items:
                    # Convert API items to string-value dicts matching field format
                    converted: list[dict[str, str | None]] = []
                    for raw in api_items[:1]:  # preview needs 1
                        record: dict[str, str | None] = {}
                        for k, v in raw.items():
                            record[k] = str(v) if v is not None else None
                        converted.append(record)
                    listing_api_items = converted
                    plan.listing_api_plan = ListingApiPlan(
                        api_url=api_url or "",
                        items_json_path=json_path,
                        sample_items=api_items[:3],
                        total_count=len(api_items),
                    )
            except Exception as exc:
                log.warning("Listing API interception failed: %s", exc)
            log.info("Listing API interception finished in %.1fs (%d items)", _time.monotonic() - t0, len(listing_api_items))

        log.info(
            "Preview — CSS: %d items, Smart: %d items, Crawl4AI: %d items, "
            "UniversalScraper: %d items, ListingAPI: %d items",
            len(css_items), len(smart_items), len(crawl4ai_items),
            len(us_items), len(listing_api_items),
        )

        sd = extract_all_structured_data(html)
        css_pages = [PageData(url=plan.url, items=css_items, structured_data=sd)] if css_items else []
        smart_pages = [PageData(url=plan.url, items=smart_items, structured_data=sd)] if smart_items else []
        crawl4ai_pages = [PageData(url=plan.url, items=crawl4ai_items, structured_data=sd)] if crawl4ai_items else []
        us_pages = [PageData(url=plan.url, items=us_items, structured_data=sd)] if us_items else []
        listing_api_pages = [PageData(url=plan.url, items=listing_api_items, structured_data=sd)] if listing_api_items else []

        # ── Share detail_link across methods ──────────────────────────
        # In preview each method has at most 1 item.  If any method
        # extracted a detail_link but others didn't, copy it over so all
        # methods can benefit from detail-page enrichment.
        all_method_pages = [css_pages, smart_pages, crawl4ai_pages, us_pages]
        donor_link: str | None = None
        for mp in all_method_pages:
            if mp and mp[0].items:
                link = mp[0].items[0].get("detail_link")
                if link:
                    donor_link = link
                    break
        if donor_link:
            for mp in all_method_pages:
                if mp and mp[0].items and not mp[0].items[0].get("detail_link"):
                    mp[0].items[0]["detail_link"] = donor_link
                    log.debug("Copied detail_link '%s' to %s preview item", donor_link,
                              "css" if mp is css_pages else "smart" if mp is smart_pages
                              else "crawl4ai" if mp is crawl4ai_pages else "us")

        # ── Enrich all with detail pages (1 item each) ─────────────────
        shared_detail_htmls: dict[str, str] = {}  # cache across methods
        for label, method_pages in [
            ("css", css_pages), ("smart", smart_pages),
            ("crawl4ai", crawl4ai_pages), ("us", us_pages),
        ]:
            if not (method_pages and method_pages[0].items):
                continue
            # Pre-populate with already-fetched detail HTML
            if shared_detail_htmls:
                method_pages[0].detail_pages.update(shared_detail_htmls)
            method_pages[:] = (await self._enrich_detail_pages(method_pages, plan, max_details=1)).pages
            method_pages[:] = await self._enrich_detail_api(method_pages, plan, max_details=1)
            # Collect any newly fetched detail HTML for subsequent methods
            for pd in method_pages:
                for url, html_content in pd.detail_pages.items():
                    if url not in shared_detail_htmls:
                        shared_detail_htmls[url] = html_content
        # Listing API items already come as structured data — no detail enrichment needed

        return css_pages, smart_pages, crawl4ai_pages, us_pages, listing_api_pages

    async def scrape_detail_urls_only(
        self,
        plan: ScrapingPlan,
        detail_urls: list[str],
        *,
        cancel_event: asyncio.Event | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> tuple[list[PageData], EnrichResult]:
        """Fetch specific detail page URLs only (for resume).

        Creates synthetic items with ``detail_link`` set to each URL, then
        runs the standard detail-page enrichment pipeline on them.
        """
        log.info("Resume scrape: fetching %d remaining detail pages", len(detail_urls))
        items = [{"detail_link": url} for url in detail_urls]
        pages = [PageData(url=plan.url, items=items)]
        enrich_result = await self._enrich_detail_pages(
            pages, plan,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
        return enrich_result.pages, enrich_result

    # ------------------------------------------------------------------
    # Preview helpers
    # ------------------------------------------------------------------
    async def _scrape_preview_static(self, plan: ScrapingPlan) -> list[PageData]:
        """Fetch the first page and extract only the first item."""
        html = await fetch_page(plan.url)
        if not html:
            return []
        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan)
        if items:
            items = items[:1]
        return [PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html))]

    async def _scrape_preview_js(self, plan: ScrapingPlan) -> list[PageData]:
        """Use Playwright to fetch the first page and extract only the first item."""
        async with get_browser() as browser:
            html = await fetch_page_js(browser, plan.url, wait_selector=plan.wait_selector)
            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan)
            if items:
                items = items[:1]
            return [PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html))]

    # ------------------------------------------------------------------
    # Static (httpx) path
    # ------------------------------------------------------------------
    async def _scrape_static(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[PageData], EnrichResult | None]:
        urls = self._resolve_page_urls(plan)

        # For NEXT_BUTTON with no pre-resolved URLs, follow links from HTML
        if (
            plan.pagination in (PaginationStrategy.NEXT_BUTTON, PaginationStrategy.PAGE_NUMBERS)
            and len(urls) <= 1
            and plan.pagination_selector
        ):
            urls = await self._follow_static_pagination(
                urls[0] if urls else plan.url,
                plan.pagination_selector,
                max_pages=settings.max_pages_per_crawl,
            )

        log.info("Static scrape: %d page URL(s)", len(urls))
        html_map = await fetch_pages(urls)

        pages: list[PageData] = []
        total_items = 0
        pages_processed = 0
        for url, html in html_map.items():
            if cancel_event and cancel_event.is_set():
                log.info("Static scraping cancelled before processing remaining pages")
                break
            if max_items is not None and total_items >= max_items:
                break
            if not html:
                pages_processed += 1
                self._emit_page_progress(
                    progress_callback,
                    method=extraction_method,
                    page_url=url,
                    page_items=0,
                    pages_processed=pages_processed,
                    total_items=total_items,
                )
                continue
            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
            if max_items is not None:
                items = items[: max_items - total_items]
            total_items += len(items)
            pages_processed += 1
            self._emit_page_progress(
                progress_callback,
                method=extraction_method,
                page_url=url,
                page_items=len(items),
                pages_processed=pages_processed,
                total_items=total_items,
            )
            pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))

        # Detail page enrichment
        enrich_result = await self._enrich_detail_pages(pages, plan, max_details=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
        pages = enrich_result.pages
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
        return pages, enrich_result

    # ------------------------------------------------------------------
    # universal-scraper path
    # ------------------------------------------------------------------
    async def _scrape_universal_scraper(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[PageData], EnrichResult | None]:
        """Scrape pages using universal-scraper for AI-powered extraction.

        universal-scraper handles its own fetching (via Selenium) and
        extraction (via AI-generated BS4 code).  Falls back to the
        JS/static path if universal-scraper fails.
        """
        from app.utils.universal_scraper import universal_scraper_extract_batch

        urls = self._resolve_page_urls(plan)
        log.info("universal-scraper scrape: %d page URL(s)", len(urls))

        fields = list(plan.target.field_selectors.keys())
        if plan.target.detail_link_selector and "detail_link" not in fields:
            fields.append("detail_link")

        us_results = await universal_scraper_extract_batch(urls, fields=fields)

        # Check if universal-scraper returned any results
        successful = {u: items for u, items in us_results.items() if items}
        if not successful:
            log.warning(
                "universal-scraper returned no results for %d URLs — falling back to %s path",
                len(urls),
                "JS" if plan.requires_javascript else "static",
            )
            if plan.requires_javascript:
                return await self._scrape_js(
                    plan, max_items=max_items,
                    extraction_method=extraction_method,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            return await self._scrape_static(
                plan, max_items=max_items,
                extraction_method=extraction_method,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )

        pages: list[PageData] = []
        total_items = 0
        pages_processed = 0
        for url in urls:
            if cancel_event and cancel_event.is_set():
                log.info("universal-scraper cancelled before processing remaining pages")
                break
            if max_items is not None and total_items >= max_items:
                break
            items_raw = successful.get(url)
            if not items_raw:
                pages_processed += 1
                self._emit_page_progress(
                    progress_callback, method=extraction_method,
                    page_url=url, page_items=0,
                    pages_processed=pages_processed, total_items=total_items,
                )
                continue

            # Normalize to list of dicts with string values
            items: list[dict[str, str | None]] = []
            for raw in items_raw:
                item = {k: str(v) if v is not None else None for k, v in raw.items()}
                items.append(item)

            if max_items is not None:
                items = items[: max_items - total_items]
            total_items += len(items)
            pages_processed += 1
            self._emit_page_progress(
                progress_callback, method=extraction_method,
                page_url=url, page_items=len(items),
                pages_processed=pages_processed, total_items=total_items,
            )
            pages.append(PageData(url=url, items=items))

        log.info("universal-scraper scrape: extracted %d items from %d page(s)", total_items, len(pages))

        # Detail page enrichment
        enrich_result = await self._enrich_detail_pages(
            pages, plan, max_details=max_items,
            progress_callback=progress_callback, cancel_event=cancel_event,
        )
        pages = enrich_result.pages
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
        return pages, enrich_result

    # ------------------------------------------------------------------
    # Crawl4AI path
    # ------------------------------------------------------------------
    async def _scrape_crawl4ai(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[PageData], EnrichResult | None]:
        """Scrape pages using Crawl4AI for fetching (markdown + HTML).

        Compatible with pagination strategies that have pre-computed URLs
        (none, page_numbers, next_button).  Fetches each URL via Crawl4AI,
        then extracts items using either:
        - Crawl4AI's own LLM extraction (when extraction_method == CRAWL4AI)
        - Markdown-enhanced SmartScraper (when markdown is available)
        - CSS selectors on the HTML fallback

        Falls back to JS/static path if Crawl4AI fails.
        """
        from app.utils.crawl4ai import crawl4ai_fetch_batch

        urls = self._resolve_page_urls(plan)
        log.info("Crawl4AI scrape: %d page URL(s)", len(urls))

        c4_results = await crawl4ai_fetch_batch(urls)

        successful = {u: doc for u, doc in c4_results.items() if doc is not None}
        if not successful:
            log.warning(
                "Crawl4AI returned no results for %d URLs — falling back to %s path",
                len(urls),
                "JS" if plan.requires_javascript else "static",
            )
            if plan.requires_javascript:
                return await self._scrape_js(
                    plan, max_items=max_items,
                    extraction_method=extraction_method,
                    progress_callback=progress_callback,
                    cancel_event=cancel_event,
                )
            return await self._scrape_static(
                plan, max_items=max_items,
                extraction_method=extraction_method,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )

        # Fall back to httpx for URLs that Crawl4AI missed
        failed_urls = [u for u in urls if u not in successful]
        if failed_urls:
            log.info("Crawl4AI missed %d URL(s), fetching via fallback", len(failed_urls))
            fallback_htmls = await fetch_pages(failed_urls)
            for u, html in fallback_htmls.items():
                if html:
                    successful[u] = {"html": html, "markdown": ""}

        pages: list[PageData] = []
        total_items = 0
        pages_processed = 0
        for url in urls:
            if cancel_event and cancel_event.is_set():
                log.info("Crawl4AI scraping cancelled before processing remaining pages")
                break
            if max_items is not None and total_items >= max_items:
                break
            doc = successful.get(url)
            if not doc:
                pages_processed += 1
                self._emit_page_progress(
                    progress_callback, method=extraction_method,
                    page_url=url, page_items=0,
                    pages_processed=pages_processed, total_items=total_items,
                )
                continue

            html = doc.get("html", "")
            markdown = doc.get("markdown", "")

            # Choose extraction approach based on method
            if extraction_method == ExtractionMethod.CRAWL4AI and markdown:
                # Use markdown-enhanced LLM extraction
                from app.utils.smart_scraper import smart_extract_items_from_markdown

                fields = list(plan.target.field_selectors.keys())
                if plan.target.detail_link_selector and "detail_link" not in fields:
                    fields.append("detail_link")
                items = await smart_extract_items_from_markdown(markdown, fields)

                # innerText fallback for shadow-DOM pages where markdown is useless
                if not items and settings.use_inner_text_fallback:
                    try:
                        async with get_browser() as _browser:
                            _result = await fetch_page_js(
                                _browser, url, wait_selector=plan.wait_selector,
                                capture_inner_text=True,
                            )
                            _, _inner = _result  # type: ignore[misc]
                        if _inner and len(_inner) > 500:
                            log.info(
                                "Crawl4AI markdown yielded 0 items for %s — trying innerText (%d chars)",
                                url, len(_inner),
                            )
                            items = await smart_extract_items_from_markdown(_inner, fields)
                    except Exception as exc:
                        log.warning("innerText fallback failed for %s: %s", url, exc)

                if items and plan.target.detail_link_selector and html:
                    self._backfill_detail_links(html, items, plan.target)
            elif html:
                # Use standard extraction with fallback on the HTML
                items = await self._extract_items_with_fallback(
                    html, plan.target, plan.detail_api_plan,
                    extraction_method=extraction_method,
                )
            else:
                items = []

            if max_items is not None:
                items = items[: max_items - total_items]
            total_items += len(items)
            pages_processed += 1
            self._emit_page_progress(
                progress_callback, method=extraction_method,
                page_url=url, page_items=len(items),
                pages_processed=pages_processed, total_items=total_items,
            )
            pages.append(PageData(
                url=url, items=items,
                structured_data=extract_all_structured_data(html) if html else {},
            ))

        log.info("Crawl4AI scrape: extracted %d items from %d page(s)", total_items, len(pages))

        # Detail page enrichment
        enrich_result = await self._enrich_detail_pages(
            pages, plan, max_details=max_items,
            progress_callback=progress_callback, cancel_event=cancel_event,
        )
        pages = enrich_result.pages
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
        return pages, enrich_result

    # ------------------------------------------------------------------
    # JS-rendered (Playwright) path
    # ------------------------------------------------------------------
    async def _scrape_js(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[list[PageData], EnrichResult | None]:
        pages: list[PageData] = []
        total_items = 0
        pages_processed = 0

        def _limit_reached() -> bool:
            return max_items is not None and total_items >= max_items

        async with get_browser() as browser:
            match plan.pagination:
                case PaginationStrategy.NONE:
                    # Single page — wait briefly for JS content to settle
                    page = await create_page(browser)
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)
                        log.info("Single page mode — waiting for content to settle")
                        await asyncio.sleep(3)
                        html = await page.content()
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[:max_items]
                        total_items += len(items)
                        pages_processed += 1
                        self._emit_page_progress(
                            progress_callback,
                            method=extraction_method,
                            page_url=plan.url,
                            page_items=len(items),
                            pages_processed=pages_processed,
                            total_items=total_items,
                        )
                        log.info("Single page extracted %d items", len(items))
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))
                    finally:
                        await page.close()

                case PaginationStrategy.ALPHABET_TABS if plan.alphabet_tab_selector:
                    htmls = await click_all_tabs(
                        browser,
                        plan.url,
                        plan.alphabet_tab_selector,
                        wait_selector=plan.wait_selector,
                        max_items=max_items,
                        inner_pagination_selector=plan.inner_pagination_selector,
                        pagination_urls=plan.pagination_urls or None,
                    )
                    for html in htmls:
                        if cancel_event and cancel_event.is_set():
                            break
                        if _limit_reached():
                            break
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[: max_items - total_items]
                        total_items += len(items)
                        pages_processed += 1
                        self._emit_page_progress(
                            progress_callback,
                            method=extraction_method,
                            page_url=plan.url,
                            page_items=len(items),
                            pages_processed=pages_processed,
                            total_items=total_items,
                        )
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))

                case PaginationStrategy.INFINITE_SCROLL:
                    page = await create_page(browser)
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)
                        await scroll_to_bottom(page, max_scrolls=settings.max_pages_per_crawl)
                        html = await page.content()
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[:max_items]
                        total_items += len(items)
                        pages_processed += 1
                        self._emit_page_progress(
                            progress_callback,
                            method=extraction_method,
                            page_url=plan.url,
                            page_items=len(items),
                            pages_processed=pages_processed,
                            total_items=total_items,
                        )
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))
                    finally:
                        await page.close()

                case PaginationStrategy.LOAD_MORE_BUTTON if plan.pagination_selector:
                    page = await create_page(browser)
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)
                        await click_load_more(page, plan.pagination_selector)
                        html = await page.content()
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[:max_items]
                        total_items += len(items)
                        pages_processed += 1
                        self._emit_page_progress(
                            progress_callback,
                            method=extraction_method,
                            page_url=plan.url,
                            page_items=len(items),
                            pages_processed=pages_processed,
                            total_items=total_items,
                        )
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))
                    finally:
                        await page.close()

                case PaginationStrategy.NEXT_BUTTON if plan.pagination_selector:
                    page = await create_page(browser)
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)

                        visited_urls: set[str] = set()
                        for _ in range(settings.max_pages_per_crawl):
                            if cancel_event and cancel_event.is_set():
                                break
                            if _limit_reached():
                                break
                            html = await page.content()
                            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                            if max_items is not None:
                                items = items[: max_items - total_items]
                            total_items += len(items)
                            pages_processed += 1
                            self._emit_page_progress(
                                progress_callback,
                                method=extraction_method,
                                page_url=page.url,
                                page_items=len(items),
                                pages_processed=pages_processed,
                                total_items=total_items,
                            )
                            pages.append(PageData(url=page.url, items=items, structured_data=extract_all_structured_data(html)))
                            visited_urls.add(page.url)
                            if _limit_reached():
                                break

                            # --- Navigate to the next page ---
                            # Wrap the entire navigation block so that an
                            # unexpected error (e.g. invalid selector) only
                            # stops pagination instead of discarding items.
                            try:
                                # Try href-based navigation first: if the
                                # pagination element is an <a> with a real
                                # href, navigate directly instead of relying
                                # on click (avoids AJAX click-handler issues).
                                navigated = False
                                try:
                                    next_href = await page.evaluate(
                                        """(selector) => {
                                            const els = document.querySelectorAll(selector);
                                            for (const el of els) {
                                                const href = el.getAttribute('href');
                                                if (href && href !== '#' &&
                                                    !href.startsWith('javascript:')) {
                                                    return new URL(href, document.baseURI).href;
                                                }
                                            }
                                            return null;
                                        }""",
                                        plan.pagination_selector,
                                    )
                                    if next_href and next_href not in visited_urls:
                                        log.info("Next-button href navigation: %s", next_href)
                                        await page.goto(next_href, wait_until="commit", timeout=120_000)
                                        navigated = True
                                    elif next_href and next_href in visited_urls:
                                        # All hrefs already visited — try
                                        # finding an unvisited one among all
                                        # matching links.
                                        all_hrefs = await page.evaluate(
                                            """(selector) => {
                                                return [...document.querySelectorAll(selector)]
                                                    .map(el => {
                                                        const h = el.getAttribute('href');
                                                        if (h && h !== '#' &&
                                                            !h.startsWith('javascript:'))
                                                            return new URL(h, document.baseURI).href;
                                                        return null;
                                                    })
                                                    .filter(Boolean);
                                            }""",
                                            plan.pagination_selector,
                                        )
                                        unvisited = [h for h in all_hrefs if h not in visited_urls]
                                        if unvisited:
                                            log.info("Next-button href navigation (unvisited): %s", unvisited[0])
                                            await page.goto(unvisited[0], wait_until="commit", timeout=120_000)
                                            navigated = True
                                except Exception as nav_exc:
                                    log.debug("href navigation attempt failed: %s", nav_exc)

                                if not navigated:
                                    if not await safe_click(page, plan.pagination_selector):
                                        break
                            except Exception as pag_exc:
                                log.warning("Pagination failed — stopping with %d item(s) collected: %s", total_items, pag_exc)
                                break

                            await asyncio.sleep(1.5)
                            if plan.wait_selector:
                                try:
                                    await page.wait_for_selector(plan.wait_selector, timeout=10_000)
                                except Exception:
                                    pass
                    finally:
                        await page.close()

                case PaginationStrategy.PAGE_NUMBERS if plan.pagination_selector:
                    # Page-number pagination: use pre-resolved URLs first,
                    # then click the pagination button to discover remaining pages.
                    urls = self._resolve_page_urls(plan)
                    page_obj = await create_page(browser)
                    try:
                        # Phase 1: visit each pre-resolved URL
                        for idx, url in enumerate(urls[: settings.max_pages_per_crawl]):
                            if cancel_event and cancel_event.is_set():
                                break
                            if _limit_reached():
                                break
                            log.info("Pagination page %d/%d: %s", idx + 1, len(urls), url)
                            await page_obj.goto(url, wait_until="commit", timeout=120_000)
                            if plan.wait_selector:
                                try:
                                    await page_obj.wait_for_selector(plan.wait_selector, timeout=15_000)
                                except Exception:
                                    log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)
                            html = await page_obj.content()
                            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                            if max_items is not None:
                                items = items[: max_items - total_items]
                            total_items += len(items)
                            pages_processed += 1
                            self._emit_page_progress(
                                progress_callback,
                                method=extraction_method,
                                page_url=url,
                                page_items=len(items),
                                pages_processed=pages_processed,
                                total_items=total_items,
                            )
                            pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))
                            await asyncio.sleep(settings.request_delay_ms / 1000)

                        # Phase 2: always try clicking the pagination button
                        # to discover pages the LLM missed.  Exit when button
                        # disappears or clicking yields no new items.
                        pages_visited = len(pages)
                        if not _limit_reached():
                            log.info(
                                "Phase 2: clicking pagination button to discover more pages "
                                "(have %d items from %d pre-resolved URLs)",
                                total_items, len(urls),
                            )
                            for _ in range(settings.max_pages_per_crawl - pages_visited):
                                if cancel_event and cancel_event.is_set():
                                    break
                                if _limit_reached():
                                    break
                                if not await safe_click(page_obj, plan.pagination_selector):
                                    log.info("No more next-page buttons found — pagination complete")
                                    break
                                await asyncio.sleep(1.5)
                                if plan.wait_selector:
                                    try:
                                        await page_obj.wait_for_selector(plan.wait_selector, timeout=15_000)
                                    except Exception:
                                        pass
                                html = await page_obj.content()
                                current_url = page_obj.url
                                items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                                if not items:
                                    log.info("Empty page reached — stopping pagination")
                                    break
                                if max_items is not None:
                                    items = items[: max_items - total_items]
                                total_items += len(items)
                                pages_processed += 1
                                self._emit_page_progress(
                                    progress_callback,
                                    method=extraction_method,
                                    page_url=current_url,
                                    page_items=len(items),
                                    pages_processed=pages_processed,
                                    total_items=total_items,
                                )
                                pages_visited += 1
                                log.info("Discovered page %d: %s (%d items, %d total)", pages_visited, current_url, len(items), total_items)
                                pages.append(PageData(url=current_url, items=items, structured_data=extract_all_structured_data(html)))
                                await asyncio.sleep(settings.request_delay_ms / 1000)
                    finally:
                        await page_obj.close()

                case _:
                    # Single page or unknown pagination with pre-resolved URLs
                    urls = self._resolve_page_urls(plan)
                    for url in urls[: settings.max_pages_per_crawl]:
                        if cancel_event and cancel_event.is_set():
                            break
                        if _limit_reached():
                            break
                        html = await fetch_page_js(
                            browser, url, wait_selector=plan.wait_selector
                        )
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[: max_items - total_items]
                        total_items += len(items)
                        pages_processed += 1
                        self._emit_page_progress(
                            progress_callback,
                            method=extraction_method,
                            page_url=url,
                            page_items=len(items),
                            pages_processed=pages_processed,
                            total_items=total_items,
                        )
                        pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))
                        await asyncio.sleep(settings.request_delay_ms / 1000)

        # Detail page enrichment (JS path — use Playwright for detail pages too)
        enrich_result = await self._enrich_detail_pages(pages, plan, max_details=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
        pages = enrich_result.pages
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
        return pages, enrich_result

    # ------------------------------------------------------------------
    # Listing API path (intercepted XHR/fetch JSON)
    # ------------------------------------------------------------------
    async def _scrape_listing_api(
        self,
        plan: ScrapingPlan,
        *,
        max_items: int | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> list[PageData]:
        """Fetch items from an intercepted listing API URL.

        Uses the ``ListingApiPlan`` captured during preview to fetch the
        JSON endpoint directly via httpx.  Falls back to JS/static path
        if the API fetch fails.
        """
        api_plan = plan.listing_api_plan
        if not api_plan:
            log.warning("_scrape_listing_api called without listing_api_plan — falling back")
            if plan.requires_javascript:
                pages, _ = await self._scrape_js(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
            else:
                pages, _ = await self._scrape_static(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
            return pages

        log.info("Listing API scrape: %s (json_path=%s)", api_plan.api_url, api_plan.items_json_path)

        pages: list[PageData] = []
        try:
            async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
                resp = await client.get(api_plan.api_url)
                resp.raise_for_status()
                data = resp.json()

            # Navigate to the items list using the stored json_path
            items_raw: list[dict] = []
            if api_plan.items_json_path:
                obj = data
                for key in api_plan.items_json_path.split("."):
                    if isinstance(obj, dict) and key in obj:
                        obj = obj[key]
                    else:
                        obj = None
                        break
                if isinstance(obj, list):
                    items_raw = obj
            elif isinstance(data, list):
                items_raw = data
            else:
                # Try common wrapper keys
                from app.utils.browser import _LIST_CONTAINER_KEYS
                for key in _LIST_CONTAINER_KEYS:
                    if isinstance(data, dict) and key in data and isinstance(data[key], list):
                        items_raw = data[key]
                        break

            if not items_raw:
                log.warning("Listing API returned no items — falling back")
                if plan.requires_javascript:
                    pages, _ = await self._scrape_js(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
                else:
                    pages, _ = await self._scrape_static(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
                return pages

            # Convert items to string dicts
            items = [
                {k: str(v) if v is not None else None for k, v in item.items()}
                for item in items_raw
                if isinstance(item, dict)
            ]

            if max_items is not None:
                items = items[:max_items]

            pages.append(PageData(url=api_plan.api_url, items=items))
            log.info("Listing API: extracted %d items", len(items))

            if progress_callback:
                self._emit_page_progress(
                    progress_callback,
                    method=ExtractionMethod.LISTING_API,
                    page_url=api_plan.api_url,
                    page_items=len(items),
                    pages_processed=1,
                    total_items=len(items),
                )

        except Exception as exc:
            log.warning("Listing API fetch failed: %s — falling back", exc)
            if plan.requires_javascript:
                pages, _ = await self._scrape_js(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)
            else:
                pages, _ = await self._scrape_static(plan, max_items=max_items, progress_callback=progress_callback, cancel_event=cancel_event)

        return pages

    # ------------------------------------------------------------------
    # API endpoint path
    # ------------------------------------------------------------------
    async def _scrape_api(
        self, plan: ScrapingPlan, *, max_items: int | None = None
    ) -> list[PageData]:
        """Fetch data directly from a discovered JSON API."""
        log.info("Scraping API endpoint: %s", plan.api_endpoint)
        pages: list[PageData] = []
        page_num = 0
        total_collected = 0
        async with httpx.AsyncClient(timeout=settings.request_timeout_s) as client:
            while page_num < settings.max_pages_per_crawl:
                params = {**plan.api_params, plan.api_page_param: str(page_num + plan.api_page_start)}
                resp = await client.get(plan.api_endpoint, params=params)
                if resp.status_code != 200:
                    break
                try:
                    data = resp.json()
                except Exception:
                    break

                # Flatten the JSON items into string dicts for downstream parsing
                items_raw: list[dict] = []
                if isinstance(data, list):
                    items_raw = data
                elif isinstance(data, dict):
                    # Try common wrapper keys (ordered most-generic first)
                    for key in (
                        "data", "items", "results", "records", "list",
                        "hits", "rows", "content", "response", "payload",
                        "members", "companies", "organizations", "people",
                        "vendors", "sellers", "exhibitors", "entries",
                    ):
                        if key in data and isinstance(data[key], list):
                            items_raw = data[key]
                            break
                    if not items_raw:
                        items_raw = [data]

                if not items_raw:
                    break

                items = [{k: str(v) if v is not None else None for k, v in item.items()} for item in items_raw]

                if max_items is not None:
                    remaining = max_items - total_collected
                    items = items[:remaining]

                pages.append(PageData(url=f"{plan.api_endpoint}?page={page_num}", items=items))
                total_collected += len(items)
                page_num += 1

                if max_items is not None and total_collected >= max_items:
                    break

                await asyncio.sleep(settings.request_delay_ms / 1000)

        return pages

    # ------------------------------------------------------------------
    # Unified detail-page enrichment
    # ------------------------------------------------------------------
    async def _enrich_detail_pages(
        self,
        pages: list[PageData],
        plan: ScrapingPlan,
        *,
        max_details: int | None = None,
        progress_callback: Callable[[dict], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> EnrichResult:
        """Collect detail-page URLs from items and fetch them.

        Works for both static and JS paths.  Uses the ``detail_link_selector``
        from the plan to find links within each item, or falls back to any
        field whose value looks like a relative/absolute URL with 'detail' in it.

        Returns an ``EnrichResult`` with the enriched pages and lists of
        fetched/remaining URLs for resume tracking.  When *cancel_event* is
        set between batches, fetching stops gracefully and remaining URLs
        are recorded.
        """
        # If no CSS selector for detail links, check whether any items
        # already carry a detail_link (e.g. extracted by the LLM).  Only
        # skip enrichment when there is truly nothing to work with.
        if not plan.target.detail_link_selector:
            has_existing = any(
                item.get("detail_link")
                for pd in pages
                for item in pd.items
            )
            if not has_existing:
                log.debug("No detail_link_selector in plan and no items have detail_link — skipping detail enrichment")
                return EnrichResult(pages=pages)
            log.info("No detail_link_selector but %d item(s) have detail_link — proceeding with enrichment",
                     sum(1 for pd in pages for item in pd.items if item.get("detail_link")))

        # Collect detail URLs from items
        all_detail_urls: list[str] = []
        base_parsed = urlparse(plan.url)
        base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

        for pd in pages:
            for item in pd.items:
                # Try "detail_link" first, then check all fields for URL-like values
                link = item.get("detail_link")
                if not link:
                    # Fallback: look for any field containing a relative/absolute URL
                    # that looks like a detail page link
                    for key, val in item.items():
                        if val and isinstance(val, str) and (
                            val.startswith("/") or val.startswith("http")
                        ) and any(kw in key.lower() for kw in ("detail", "link", "url", "href", "profile")):
                            link = val
                            item["detail_link"] = link
                            log.debug("Using field '%s' as detail link: %s", key, link)
                            break
                if link:
                    if link.startswith("/"):
                        link = base_origin + link
                    elif not link.startswith("http"):
                        link = urljoin(plan.url, link)
                    item["detail_link"] = link
                    all_detail_urls.append(link)

        if not all_detail_urls:
            log.debug(
                "No detail URLs found in %d items (fields: %s)",
                sum(len(pd.items) for pd in pages),
                list(pages[0].items[0].keys()) if pages and pages[0].items else "empty",
            )
            return EnrichResult(pages=pages)

        if max_details is not None:
            all_detail_urls = all_detail_urls[:max_details]

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_urls: list[str] = []
        for u in all_detail_urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        total_detail = len(unique_urls)
        log.info("Enriching %d detail page(s)", total_detail)

        # Per-page timeout: 60 seconds per detail page to prevent hangs
        DETAIL_PAGE_TIMEOUT_S = 60

        # Track fetched/remaining for resume
        fetched_urls: list[str] = []
        remaining_urls: list[str] = []

        # Fetch detail pages in batches (check cancel_event between batches)
        detail_htmls: dict[str, str] = {}
        if plan.requires_javascript:
            concurrency = min(settings.max_concurrent_requests, 3)
            sem = asyncio.Semaphore(concurrency)

            async def _fetch_one(browser, url: str, idx: int) -> tuple[str, str | None]:
                async with sem:
                    if progress_callback:
                        progress_callback({
                            "stage": "enriching_details",
                            "detail_current": idx,
                            "detail_total": total_detail,
                            "detail_url": url,
                        })
                    try:
                        html = await asyncio.wait_for(
                            fetch_page_js(browser, url),
                            timeout=DETAIL_PAGE_TIMEOUT_S,
                        )
                        log.info("Detail page %d/%d: %s", idx, total_detail, url)
                        await asyncio.sleep(settings.request_delay_ms / 1000)
                        return url, html
                    except asyncio.TimeoutError:
                        log.warning(
                            "Detail page %d/%d timed out after %ds: %s",
                            idx, total_detail, DETAIL_PAGE_TIMEOUT_S, url,
                        )
                    except Exception as exc:
                        log.warning("Failed detail page %d/%d %s: %s", idx, total_detail, url, exc)
                    return url, None

            async with get_browser() as browser:
                for batch_start in range(0, len(unique_urls), _DETAIL_BATCH_SIZE):
                    # Check for graceful cancellation between batches
                    if cancel_event and cancel_event.is_set():
                        remaining_urls = unique_urls[batch_start:]
                        log.warning(
                            "Detail enrichment cancelled after %d/%d pages (timeout). %d remaining.",
                            len(fetched_urls), total_detail, len(remaining_urls),
                        )
                        break

                    batch = unique_urls[batch_start:batch_start + _DETAIL_BATCH_SIZE]
                    results = await asyncio.gather(
                        *[_fetch_one(browser, url, batch_start + idx)
                          for idx, url in enumerate(batch, 1)]
                    )
                    for url, html in results:
                        if html is not None:
                            detail_htmls[url] = html
                            fetched_urls.append(url)

            log.info("Detail enrichment complete: fetched %d/%d pages", len(detail_htmls), total_detail)
        else:
            # Static path — batch with cancel check
            for batch_start in range(0, len(unique_urls), _DETAIL_BATCH_SIZE):
                if cancel_event and cancel_event.is_set():
                    remaining_urls = unique_urls[batch_start:]
                    log.warning(
                        "Detail enrichment cancelled after %d/%d pages (timeout). %d remaining.",
                        len(fetched_urls), total_detail, len(remaining_urls),
                    )
                    break

                batch = unique_urls[batch_start:batch_start + _DETAIL_BATCH_SIZE]
                if progress_callback:
                    progress_callback({
                        "stage": "enriching_details",
                        "detail_current": batch_start,
                        "detail_total": total_detail,
                        "detail_url": f"(batch {batch_start // _DETAIL_BATCH_SIZE + 1})",
                    })
                batch_htmls = await fetch_pages(batch)
                detail_htmls.update(batch_htmls)
                fetched_urls.extend(url for url in batch if url in batch_htmls)

            log.info("Detail enrichment complete: fetched %d/%d pages", len(detail_htmls), total_detail)

        for pd in pages:
            pd.detail_pages.update(detail_htmls)

        # Follow sub-links on detail pages if the plan defines them
        if plan.detail_page_plan and plan.detail_page_plan.sub_links:
            sub_link_data = await self._follow_detail_sub_links(
                detail_htmls, plan, max_details=max_details
            )
            for pd in pages:
                pd.detail_sub_pages.update(sub_link_data)

        return EnrichResult(
            pages=pages,
            fetched_urls=fetched_urls,
            remaining_urls=remaining_urls,
        )

    async def _follow_detail_sub_links(
        self,
        detail_htmls: dict[str, str],
        plan: ScrapingPlan,
        *,
        max_details: int | None = None,
    ) -> dict[str, dict[str, str]]:
        """For each detail page, find and follow sub-links defined in the plan.

        Returns: {detail_page_url: {sub_link_label: sub_page_html}}
        """
        if not plan.detail_page_plan or not plan.detail_page_plan.sub_links:
            return {}

        base_parsed = urlparse(plan.url)
        base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

        # Collect all sub-link URLs to fetch
        urls_to_fetch: list[tuple[str, str, str]] = []  # (detail_url, label, sub_url)

        for detail_url, html in detail_htmls.items():
            if not html:
                continue
            soup = BeautifulSoup(html, "lxml")

            for sub_link_def in plan.detail_page_plan.sub_links[
                : settings.max_sub_links_per_detail
            ]:
                el = soup.select_one(sub_link_def.selector)
                if not el:
                    continue
                sub_href = el.get(sub_link_def.attribute)
                if (
                    not sub_href
                    or sub_href.startswith("#")
                    or sub_href.startswith("javascript:")
                ):
                    continue
                # Resolve relative URL
                if sub_href.startswith("/"):
                    sub_href = base_origin + sub_href
                elif not sub_href.startswith("http"):
                    sub_href = urljoin(detail_url, sub_href)
                # Only follow same-domain links
                if urlparse(sub_href).netloc != base_parsed.netloc:
                    continue
                urls_to_fetch.append((detail_url, sub_link_def.label, sub_href))

        if not urls_to_fetch:
            return {}

        if max_details is not None:
            max_sub = max_details * settings.max_sub_links_per_detail
            urls_to_fetch = urls_to_fetch[:max_sub]

        log.info("Following %d sub-link(s) across detail pages", len(urls_to_fetch))

        # Deduplicate sub-URLs for fetching
        unique_sub_urls = list({t[2] for t in urls_to_fetch})
        sub_htmls: dict[str, str] = {}
        if plan.requires_javascript:
            async with get_browser() as browser:
                for url in unique_sub_urls:
                    try:
                        sub_htmls[url] = await fetch_page_js(browser, url)
                    except Exception as exc:
                        log.warning("Failed sub-link page %s: %s", url, exc)
                    await asyncio.sleep(settings.request_delay_ms / 1000)
        else:
            sub_htmls = await fetch_pages(unique_sub_urls)

        # Organise results by detail page URL
        result: dict[str, dict[str, str]] = {}
        for detail_url, label, sub_url in urls_to_fetch:
            if sub_url in sub_htmls and sub_htmls[sub_url]:
                result.setdefault(detail_url, {})[label] = sub_htmls[sub_url]

        return result

    # ------------------------------------------------------------------
    # API-based detail enrichment
    # ------------------------------------------------------------------
    async def _enrich_detail_api(
        self,
        pages: list[PageData],
        plan: ScrapingPlan,
        *,
        max_details: int | None = None,
    ) -> list[PageData]:
        """Fetch detail data via API calls using a discovered URL template.

        For sites that require JavaScript (session cookies, auth tokens set by
        the browser), uses Playwright to make API calls within the browser
        context so cookies are automatically included.  Falls back to httpx for
        static/cookie-free APIs.
        """
        if not plan.detail_api_plan:
            return pages

        template = plan.detail_api_plan.api_url_template

        # Collect (page_idx, item_idx, item_id, api_url) tuples
        api_calls: list[tuple[int, int, str, str]] = []
        for page_idx, pd in enumerate(pages):
            for item_idx, item in enumerate(pd.items):
                item_id = item.get("_detail_api_id")
                if not item_id:
                    continue
                api_url = template.replace("{id}", item_id)
                api_calls.append((page_idx, item_idx, item_id, api_url))

        if not api_calls:
            log.warning("No item IDs found for API detail enrichment")
            return pages

        if max_details is not None:
            api_calls = api_calls[:max_details]

        log.info("Fetching %d detail API response(s)", len(api_calls))

        # Deduplicate URLs for fetching
        unique_urls = list({t[3] for t in api_calls})
        api_responses: dict[str, dict] = {}

        if plan.requires_javascript:
            # Use Playwright so session cookies are included in API requests
            api_responses = await self._fetch_detail_apis_via_browser(plan.url, unique_urls)
        else:
            # Static path: httpx with browser-like headers
            from urllib.parse import urlparse

            parsed = urlparse(plan.url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            api_headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": plan.url,
                "Origin": origin,
            }
            semaphore = asyncio.Semaphore(settings.max_concurrent_requests)

            async def _fetch_one(api_url: str) -> None:
                async with semaphore:
                    try:
                        async with httpx.AsyncClient(
                            timeout=settings.request_timeout_s,
                            follow_redirects=True,
                        ) as client:
                            resp = await client.get(api_url, headers=api_headers)
                            if resp.status_code == 200:
                                api_responses[api_url] = resp.json()
                            else:
                                log.warning("Detail API %s returned %d", api_url, resp.status_code)
                    except Exception as exc:
                        log.warning("Failed detail API call %s: %s", api_url, exc)
                    await asyncio.sleep(settings.request_delay_ms / 1000)

            await asyncio.gather(*[_fetch_one(url) for url in unique_urls])

        # Attach responses to PageData
        for page_idx, item_idx, item_id, api_url in api_calls:
            if api_url in api_responses:
                pages[page_idx].detail_api_responses[item_id] = api_responses[api_url]

        log.info("Enriched %d items with API detail data", len(api_responses))
        return pages

    async def _fetch_detail_apis_via_browser(
        self,
        listing_url: str,
        api_urls: list[str],
    ) -> dict[str, dict]:
        """Fetch JSON API URLs using a Playwright browser session.

        Navigates to the listing page first so session cookies are established,
        then uses ``fetch()`` inside the page context to call each API URL with
        those cookies automatically included.
        """
        results: dict[str, dict] = {}
        async with get_browser() as browser:
            page = await create_page(browser)
            try:
                # Prime the session: load listing page to set cookies / tokens
                log.info("Priming browser session via %s", listing_url)
                await page.goto(listing_url, wait_until="commit", timeout=120_000)
                await asyncio.sleep(2)

                for api_url in api_urls:
                    try:
                        data = await page.evaluate(
                            """async (url) => {
                                const resp = await fetch(url, {
                                    credentials: "include",
                                    headers: {
                                        "Accept": "application/json, text/plain, */*",
                                        "Referer": window.location.href
                                    }
                                });
                                if (!resp.ok) return null;
                                return await resp.json();
                            }""",
                            api_url,
                        )
                        if data and isinstance(data, dict):
                            results[api_url] = data
                            log.debug("Browser-fetched API %s — %d keys", api_url, len(data))
                        else:
                            log.warning("Browser API fetch returned no data: %s", api_url)
                    except Exception as exc:
                        log.warning("Browser API fetch failed %s: %s", api_url, exc)
                    await asyncio.sleep(settings.request_delay_ms / 1000)
            finally:
                await page.close()
        return results

    # ------------------------------------------------------------------
    # Scrapy (subprocess) path
    # ------------------------------------------------------------------
    def _can_use_scrapy(self, plan: ScrapingPlan) -> bool:
        """Check if this plan can be handled by the Scrapy spider.

        Scrapy handles: none, next_button, page_numbers pagination.
        Playwright-only: infinite_scroll, load_more_button, alphabet_tabs.
        """
        scrapy_compatible = {
            PaginationStrategy.NONE,
            PaginationStrategy.NEXT_BUTTON,
            PaginationStrategy.PAGE_NUMBERS,
        }
        return plan.pagination in scrapy_compatible

    async def _scrape_scrapy(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
    ) -> list[PageData]:
        """Run the Scrapy PlanSpider in a subprocess and convert results to PageData."""
        log.info("Starting Scrapy subprocess for %s", plan.url)

        plan_dict = plan.model_dump(mode="json")
        plan_file = Path(tempfile.mktemp(suffix=".json", prefix="scrapy_plan_"))
        output_file = Path(tempfile.mktemp(suffix=".json", prefix="scrapy_output_"))

        proc = None
        try:
            plan_file.write_text(
                json.dumps(plan_dict, ensure_ascii=False), encoding="utf-8"
            )

            # Pass Scrapy settings through environment variables
            env = {**os.environ}
            env["SCRAPY_CONCURRENT_REQUESTS"] = str(settings.scrapy_concurrent_requests)
            env["SCRAPY_CONCURRENT_REQUESTS_PER_DOMAIN"] = str(
                settings.scrapy_concurrent_requests_per_domain
            )
            env["SCRAPY_DOWNLOAD_DELAY"] = str(settings.scrapy_download_delay)
            env["SCRAPY_RANDOMIZE_DELAY"] = str(settings.scrapy_randomize_delay).lower()
            env["SCRAPY_RETRY_TIMES"] = str(settings.scrapy_retry_times)
            env["SCRAPY_RETRY_HTTP_CODES"] = json.dumps(settings.scrapy_retry_http_codes)
            env["SCRAPY_OBEY_ROBOTSTXT"] = str(settings.scrapy_obey_robotstxt).lower()
            env["SCRAPY_AUTOTHROTTLE_ENABLED"] = str(
                settings.scrapy_autothrottle_enabled
            ).lower()
            env["SCRAPY_AUTOTHROTTLE_START_DELAY"] = str(
                settings.scrapy_autothrottle_start_delay
            )
            env["SCRAPY_AUTOTHROTTLE_MAX_DELAY"] = str(
                settings.scrapy_autothrottle_max_delay
            )
            env["SCRAPY_AUTOTHROTTLE_TARGET_CONCURRENCY"] = str(
                settings.scrapy_autothrottle_target_concurrency
            )
            env["SCRAPY_DOWNLOAD_TIMEOUT"] = str(settings.request_timeout_s)
            env["SCRAPY_LOG_LEVEL"] = settings.log_level

            cmd = [
                sys.executable, "-m", "app.scrapy_runner.run",
                "--plan", str(plan_file),
                "--output", str(output_file),
            ]
            if max_items is not None:
                cmd.extend(["--max-items", str(max_items)])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.scrapy_subprocess_timeout_s,
            )

            if proc.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace")[-2000:]
                log.error(
                    "Scrapy subprocess failed (rc=%d): %s", proc.returncode, err_msg
                )
                raise RuntimeError(
                    f"Scrapy subprocess exited with code {proc.returncode}"
                )

            if not output_file.exists():
                log.warning("Scrapy output file not found: %s", output_file)
                return []

            raw_items = json.loads(output_file.read_text(encoding="utf-8"))
            log.info("Scrapy subprocess returned %d items", len(raw_items))

            return self._convert_scrapy_items_to_page_data(raw_items, plan)

        except asyncio.TimeoutError:
            log.error(
                "Scrapy subprocess timed out after %ds",
                settings.scrapy_subprocess_timeout_s,
            )
            if proc and proc.returncode is None:
                proc.kill()
            raise

        finally:
            if plan_file.exists():
                plan_file.unlink()
            if output_file.exists():
                output_file.unlink()

    def _convert_scrapy_items_to_page_data(
        self, raw_items: list[dict], plan: ScrapingPlan,
    ) -> list[PageData]:
        """Convert Scrapy spider output into PageData objects.

        Groups items by their source listing page URL.
        Extracts _detail_html into the detail_pages dict.
        """
        pages_map: dict[str, PageData] = {}

        for raw in raw_items:
            if raw.get("type") != "item":
                continue

            source_url = raw.pop("_source_url", plan.url)
            detail_html = raw.pop("_detail_html", None)
            detail_url = raw.pop("_detail_url", None)
            raw.pop("type", None)

            if source_url not in pages_map:
                pages_map[source_url] = PageData(url=source_url, items=[])

            pd = pages_map[source_url]
            pd.items.append(raw)

            if detail_html and detail_url:
                pd.detail_pages[detail_url] = detail_html

        log.info(
            "Converted Scrapy output: %d pages, %d total items",
            len(pages_map),
            sum(len(p.items) for p in pages_map.values()),
        )
        return list(pages_map.values())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _follow_static_pagination(
        self,
        start_url: str,
        selector: str,
        *,
        max_pages: int = 200,
    ) -> list[str]:
        """Discover paginated URLs by following next-button hrefs in static HTML.

        Starts from *start_url*, fetches its HTML, extracts hrefs from
        elements matching *selector*, picks the first unvisited href, and
        repeats until no new pages are found or *max_pages* is reached.
        """
        from urllib.parse import urljoin
        urls: list[str] = [start_url]
        visited: set[str] = {start_url}

        current_url = start_url
        for _ in range(max_pages - 1):
            html = await fetch_page(current_url)
            if not html:
                break
            soup = BeautifulSoup(html, "lxml")
            try:
                links = soup.select(selector)
            except Exception:
                break

            next_url = None
            for link in links:
                href = link.get("href")
                if href and href != "#" and not href.startswith("javascript:"):
                    absolute = urljoin(current_url, href)
                    if absolute not in visited:
                        next_url = absolute
                        break

            if not next_url:
                break
            visited.add(next_url)
            urls.append(next_url)
            current_url = next_url

        log.info("Static pagination discovery: found %d page URLs", len(urls))
        return urls

    def _resolve_page_urls(self, plan: ScrapingPlan) -> list[str]:
        from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
        if plan.pagination_urls:
            resolved = [urljoin(plan.url, u) for u in plan.pagination_urls]

            # Auto-extrapolate if the LLM gave too few URLs compared to
            # total_items_hint.  Detects a numeric page parameter that
            # increments across the provided URLs and extends the list.
            if (
                plan.total_items_hint
                and len(resolved) >= 2
            ):
                # Try to detect the varying page parameter
                parsed_first = urlparse(resolved[0])
                parsed_last = urlparse(resolved[-1])
                qs_first = parse_qs(parsed_first.query, keep_blank_values=True)
                qs_last = parse_qs(parsed_last.query, keep_blank_values=True)

                page_param = None
                first_page_num = None
                last_page_num = None
                for key in qs_first:
                    if key in qs_last:
                        try:
                            v_first = int(qs_first[key][0])
                            v_last = int(qs_last[key][0])
                            if v_last > v_first:
                                page_param = key
                                first_page_num = v_first
                                last_page_num = v_last
                                break
                        except (ValueError, IndexError):
                            continue

                if page_param and last_page_num is not None and first_page_num is not None:
                    # Detect step size from the provided URLs.
                    # For offset-based pagination (start=0,20,40) step=20;
                    # for page-based (page=1,2,3) step=1.
                    if len(resolved) >= 2:
                        # Use first two URLs to detect step
                        qs_second = parse_qs(urlparse(resolved[1]).query, keep_blank_values=True)
                        try:
                            v_second = int(qs_second[page_param][0])
                            step = v_second - first_page_num
                        except (ValueError, IndexError, KeyError):
                            step = 1
                    else:
                        step = 1
                    step = max(step, 1)

                    # Estimate total pages from hint
                    items_per_page = step if step > 1 else max(plan.total_items_hint // max(len(resolved), 1), 20)
                    estimated_total_pages = (plan.total_items_hint // items_per_page) + 1
                    if estimated_total_pages > len(resolved):
                        log.info(
                            "Auto-extrapolating pagination: LLM gave %d URLs but "
                            "total_items_hint=%d suggests ~%d pages (step=%d) — extending",
                            len(resolved), plan.total_items_hint,
                            estimated_total_pages, step,
                        )
                        base_parsed = urlparse(resolved[-1])
                        base_qs = parse_qs(base_parsed.query, keep_blank_values=True)
                        next_val = last_page_num + step
                        max_val = first_page_num + estimated_total_pages * step
                        while next_val <= max_val:
                            new_qs = {k: v[0] for k, v in base_qs.items()}
                            new_qs[page_param] = str(next_val)
                            new_url = urlunparse((
                                base_parsed.scheme, base_parsed.netloc,
                                base_parsed.path, base_parsed.params,
                                urlencode(new_qs), base_parsed.fragment,
                            ))
                            resolved.append(new_url)
                            next_val += step

            return resolved[: settings.max_pages_per_crawl]
        return [plan.url]

    def _extract_items(
        self,
        html: str,
        target: ScrapingTarget,
        detail_api_plan: DetailApiPlan | None = None,
    ) -> list[dict[str, str | None]]:
        """Use BeautifulSoup + CSS selectors to pull raw field values from a page."""
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(target.item_container_selector)
        log.debug(
            "Found %d items with selector '%s'", len(containers), target.item_container_selector
        )

        items: list[dict[str, str | None]] = []
        for container in containers:
            record: dict[str, str | None] = {}
            for field, selector in target.field_selectors.items():
                if not selector or not selector.strip():
                    log.debug("Skipping field '%s' — empty CSS selector", field)
                    record[field] = None
                    continue
                try:
                    el = container.select_one(selector)
                except Exception as exc:
                    log.warning("Invalid CSS selector for field '%s': '%s' — %s", field, selector, exc)
                    record[field] = None
                    continue
                if el is None:
                    record[field] = None
                    continue
                attr = target.field_attributes.get(field)
                if attr:
                    record[field] = el.get(attr)  # type: ignore[assignment]
                else:
                    record[field] = el.get_text(separator=" ", strip=True)

            # Also extract detail link if selector is present
            if target.detail_link_selector and target.detail_link_selector.strip() and "detail_link" not in record:
                try:
                    link_el = container.select_one(target.detail_link_selector)
                    if link_el:
                        record["detail_link"] = link_el.get("href")  # type: ignore[assignment]
                except Exception as exc:
                    log.warning("Invalid detail_link_selector '%s': %s", target.detail_link_selector, exc)

            # Extract API detail ID if a detail_api_plan exists
            if detail_api_plan and detail_api_plan.id_selector and detail_api_plan.id_selector.strip() and "_detail_api_id" not in record:
                try:
                    id_el = container.select_one(detail_api_plan.id_selector)
                    if id_el:
                        if detail_api_plan.id_attribute:
                            raw_id = id_el.get(detail_api_plan.id_attribute, "")
                        else:
                            raw_id = id_el.get_text(strip=True)
                        if detail_api_plan.id_regex and raw_id:
                            match = re.search(detail_api_plan.id_regex, str(raw_id))
                            if match:
                                raw_id = match.group(1)
                        record["_detail_api_id"] = str(raw_id) if raw_id else None
                except Exception as exc:
                    log.warning("Invalid id_selector '%s': %s", detail_api_plan.id_selector, exc)

            # Skip records where every user field is None (all selectors
            # were empty or missed).  Without this, the parser invents data
            # from the raw container text (e.g. the page title).
            user_values = [
                v for k, v in record.items()
                if not k.startswith("_") and k != "detail_link"
            ]
            if any(v is not None for v in user_values):
                items.append(record)
            else:
                log.debug("Dropping empty record — all field selectors returned None")
        return items

    @staticmethod
    def _slice_html_for_preview(html: str, item_container_selector: str) -> str:
        """Return a *minimal* HTML document containing at most 3 item containers.

        Preserves the full ancestor chain (with IDs/classes) so that CSS
        selectors like ``#exhibitor-directory .filter-results .directory-item``
        still match.  Strips scripts, styles, and all sibling content to
        keep the result small.

        Falls back to the first 50 KB if the selector doesn't match.
        """
        selector = (item_container_selector or "").strip()
        if not selector:
            return html[:50_000]

        try:
            soup = BeautifulSoup(html, "lxml")
            containers = soup.select(selector)
            if not containers:
                log.debug("Preview slice: selector '%s' matched 0 elements, using first 50KB", selector)
                return html[:50_000]

            # Keep only the first 3 containers
            keep = containers[:3]

            # Walk up from the first container to <body>, collecting ancestors
            # so we can reconstruct the full selector chain.
            ancestors: list = []
            node = keep[0].parent
            while node and node.name and node.name not in ("[document]",):
                ancestors.append(node)
                node = node.parent
            ancestors.reverse()  # top-down: html → body → ... → parent

            # Remove every <script> and <style> from the whole soup first
            for tag in soup.find_all(["script", "style", "link", "noscript", "svg", "img"]):
                tag.decompose()

            # In the direct parent of the containers, remove all children
            # that are NOT one of the kept containers.
            direct_parent = keep[0].parent
            if direct_parent:
                for child in list(direct_parent.children):
                    if hasattr(child, 'name') and child not in keep:
                        child.decompose()
                    elif not hasattr(child, 'name'):
                        # NavigableString — remove if it's not just whitespace
                        pass  # keep whitespace for formatting

            # In each ancestor (except the direct parent), remove siblings
            # that are not on the path to the containers.  This includes
            # <html> (strips <head>) and <body> (strips header/nav/footer).
            for i, anc in enumerate(ancestors):
                if anc == direct_parent:
                    continue
                # The child on the path is the next ancestor (or direct_parent)
                path_child = ancestors[i + 1] if i + 1 < len(ancestors) else direct_parent
                for child in list(anc.children):
                    if hasattr(child, 'name') and child != path_child:
                        child.decompose()

            minimal = str(soup)
            log.debug(
                "Preview slice: extracted %d/%d containers, HTML %d → %d chars",
                len(keep), len(containers), len(html), len(minimal),
            )
            return minimal
        except Exception as exc:
            log.debug("Preview slice failed (%s), using first 50KB", exc)
            return html[:50_000]

    async def _extract_items_dual(
        self,
        html: str,
        target: ScrapingTarget,
        detail_api_plan: DetailApiPlan | None = None,
    ) -> tuple[list[dict[str, str | None]], list[dict[str, str | None]]]:
        """Run both CSS and SmartScraperGraph extraction concurrently.

        Returns (css_items, smart_items).  Both always run so the user can
        compare results side-by-side and pick the better one.
        """
        # CSS extraction (synchronous, fast)
        css_items = self._extract_items(html, target, detail_api_plan)
        log.info("Dual extraction — CSS found %d items", len(css_items))

        # SmartScraperGraph extraction (async, may be slow)
        smart_items: list[dict[str, str | None]] = []
        if settings.use_smart_scraper_primary and len(html) >= 500:
            from app.utils.smart_scraper import smart_extract_items

            fields = list(target.field_selectors.keys())
            if target.detail_link_selector and "detail_link" not in fields:
                fields.append("detail_link")
            smart_items = await smart_extract_items(html, fields)
            if smart_items and target.detail_link_selector:
                self._backfill_detail_links(html, smart_items, target)
            log.info("Dual extraction — SmartScraperGraph found %d items", len(smart_items))
        else:
            log.info("Dual extraction — SmartScraperGraph skipped (disabled or HTML too short)")

        return css_items, smart_items

    async def _extract_items_with_fallback(
        self,
        html: str,
        target: ScrapingTarget,
        detail_api_plan: DetailApiPlan | None = None,
        *,
        extraction_method: ExtractionMethod | None = None,
    ) -> list[dict[str, str | None]]:
        """Extract items using the specified method.

        - CSS: only CSS selectors (fast, reliable)
        - SMART_SCRAPER: SmartScraperGraph primary, CSS fallback on failure.
          For large pages the HTML is reduced to just the item containers
          (using the CSS selector from the plan) and split into chunks so the
          LLM never has to deal with a truncated page.
        - None: CSS if it finds items, else SmartScraperGraph fallback
        """
        if extraction_method == ExtractionMethod.CSS:
            items = self._extract_items(html, target, detail_api_plan)
            log.info("CSS-only extraction: %d items", len(items))
            return items

        if extraction_method == ExtractionMethod.SMART_SCRAPER:
            if settings.use_smart_scraper_primary and len(html) >= 500:
                from app.utils.smart_scraper import smart_extract_items

                fields = list(target.field_selectors.keys())
                if target.detail_link_selector and "detail_link" not in fields:
                    fields.append("detail_link")
                smart_items = await self._smart_extract_chunked(
                    html, fields, target.item_container_selector,
                )
                if smart_items:
                    if target.detail_link_selector:
                        self._backfill_detail_links(html, smart_items, target)
                    log.info("SmartScraperGraph extraction: %d items", len(smart_items))
                    return smart_items
                log.warning("SmartScraperGraph returned 0 items — falling back to CSS")
            return self._extract_items(html, target, detail_api_plan)

        if extraction_method == ExtractionMethod.CRAWL4AI:
            # When called from _extract_items_with_fallback with HTML only
            # (i.e. not from _scrape_crawl4ai which handles markdown directly),
            # fall back to SmartScraper on the HTML, then CSS.
            if settings.use_smart_scraper_primary and len(html) >= 500:
                from app.utils.smart_scraper import smart_extract_items

                fields = list(target.field_selectors.keys())
                if target.detail_link_selector and "detail_link" not in fields:
                    fields.append("detail_link")
                smart_items = await self._smart_extract_chunked(
                    html, fields, target.item_container_selector,
                )
                if smart_items:
                    if target.detail_link_selector:
                        self._backfill_detail_links(html, smart_items, target)
                    log.info("Crawl4AI fallback (SmartScraper on HTML): %d items", len(smart_items))
                    return smart_items
            return self._extract_items(html, target, detail_api_plan)

        if extraction_method == ExtractionMethod.UNIVERSAL_SCRAPER:
            # universal-scraper handles its own fetching; when called here
            # with pre-fetched HTML, fall back to SmartScraper then CSS.
            if settings.use_smart_scraper_primary and len(html) >= 500:
                from app.utils.smart_scraper import smart_extract_items

                fields = list(target.field_selectors.keys())
                if target.detail_link_selector and "detail_link" not in fields:
                    fields.append("detail_link")
                smart_items = await self._smart_extract_chunked(
                    html, fields, target.item_container_selector,
                )
                if smart_items:
                    if target.detail_link_selector:
                        self._backfill_detail_links(html, smart_items, target)
                    log.info("universal-scraper fallback (SmartScraper on HTML): %d items", len(smart_items))
                    return smart_items
            return self._extract_items(html, target, detail_api_plan)

        # None / auto — original fallback logic
        css_items = self._extract_items(html, target, detail_api_plan)

        if len(css_items) >= 1:
            log.info("CSS selectors extracted %d items — skipping SmartScraperGraph", len(css_items))
            return css_items

        if settings.use_smart_scraper_primary and len(html) >= 500:
            from app.utils.smart_scraper import smart_extract_items

            fields = list(target.field_selectors.keys())
            if target.detail_link_selector and "detail_link" not in fields:
                fields.append("detail_link")
            smart_items = await self._smart_extract_chunked(
                html, fields, target.item_container_selector,
            )
            if smart_items:
                log.info("SmartScraperGraph extracted %d items (fallback, CSS had %d)", len(smart_items), len(css_items))
                if target.detail_link_selector:
                    self._backfill_detail_links(html, smart_items, target)
                return smart_items
            log.warning(
                "SmartScraperGraph returned 0 items — using CSS results (%d items)", len(css_items)
            )

        return css_items

    async def _smart_extract_chunked(
        self,
        html: str,
        fields: list[str],
        item_container_selector: str,
    ) -> list[dict[str, str | None]]:
        """Run smart_extract_items, chunking large pages by CSS containers.

        When the full HTML exceeds 100K chars and we have a working CSS
        selector, we strip the page down to just the item containers and
        split them into LLM-sized chunks.  This avoids the blind truncation
        that loses most items on large pages.
        """
        from app.utils.smart_scraper import smart_extract_items

        _MAX = 90_000  # leave headroom vs the 100K hard limit in smart_scraper

        # If the page is small enough, just send it directly.
        if len(html) <= _MAX:
            return await smart_extract_items(html, fields)

        # --- Large page: try to reduce using CSS containers ---
        selector = (item_container_selector or "").strip()
        if not selector:
            # No selector — fall through to raw (will be truncated by smart_extract_items)
            log.warning("Large HTML (%d chars) but no container selector — LLM will see truncated page", len(html))
            return await smart_extract_items(html, fields)

        try:
            soup = BeautifulSoup(html, "lxml")
            containers = soup.select(selector)
        except Exception:
            containers = []

        if not containers:
            log.warning("Container selector '%s' matched 0 elements on large page — LLM will see truncated page", selector)
            return await smart_extract_items(html, fields)

        # Build minimal HTML per container (just outer HTML)
        container_htmls = [str(c) for c in containers]
        log.info(
            "Chunking %d containers for LLM extraction (total HTML %d chars)",
            len(container_htmls), len(html),
        )

        # Group containers into chunks that fit within _MAX chars
        chunks: list[str] = []
        current_parts: list[str] = []
        current_size = 0
        for ch in container_htmls:
            if current_size + len(ch) > _MAX and current_parts:
                chunks.append("\n".join(current_parts))
                current_parts = []
                current_size = 0
            current_parts.append(ch)
            current_size += len(ch)
        if current_parts:
            chunks.append("\n".join(current_parts))

        log.info("Split into %d chunk(s) for LLM extraction", len(chunks))

        all_items: list[dict[str, str | None]] = []
        for i, chunk in enumerate(chunks):
            items = await smart_extract_items(chunk, fields)
            log.debug("Chunk %d/%d: extracted %d items", i + 1, len(chunks), len(items))
            all_items.extend(items)

        log.info("Chunked LLM extraction total: %d items from %d containers", len(all_items), len(containers))
        return all_items

    def _backfill_detail_links(
        self,
        html: str,
        items: list[dict[str, str | None]],
        target: ScrapingTarget,
    ) -> None:
        """Ensure every item has a detail_link by matching to CSS containers via name.

        Uses fuzzy text matching instead of positional indexing because the LLM
        may extract items in a different order or skip items compared to the
        CSS containers in the DOM.
        """
        missing = [i for i, item in enumerate(items) if not item.get("detail_link")]
        if not missing:
            return

        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(target.item_container_selector)
        if not containers:
            return

        # Pre-compute text and detail link for each container
        container_texts: list[str] = []
        container_links: list[str | None] = []
        for c in containers:
            container_texts.append(
                " ".join(c.get_text(separator=" ", strip=True).lower().split())
            )
            try:
                link_el = c.select_one(target.detail_link_selector) if target.detail_link_selector else None
            except Exception:
                link_el = None
            container_links.append(link_el.get("href") if link_el else None)

        used_containers: set[int] = set()

        for idx in missing:
            item_name = (items[idx].get("name") or "").strip().lower()
            if not item_name:
                continue

            best_match: int | None = None
            best_score: float = 0.0

            for ci, ct in enumerate(container_texts):
                if ci in used_containers or container_links[ci] is None:
                    continue
                if item_name in ct:
                    score = len(item_name) / max(len(ct), 1)
                    if score > best_score:
                        best_score = score
                        best_match = ci

            if best_match is not None:
                items[idx]["detail_link"] = container_links[best_match]
                used_containers.add(best_match)
                log.debug(
                    "Backfilled detail_link for item %d ('%s') from container %d",
                    idx, items[idx].get("name", "?"), best_match,
                )
