"""ScraperAgent — Executes a ScrapingPlan and returns raw PageData.

Supports:
- Static pages via httpx (or Scrapy when enabled)
- JS-rendered pages via Playwright
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
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models.schemas import DetailApiPlan, ExtractionMethod, PageData, PaginationStrategy, ScrapingPlan, ScrapingTarget
from app.utils.browser import (
    click_all_tabs,
    click_load_more,
    fetch_page_js,
    get_browser,
    scroll_to_bottom,
)
from app.utils.http import fetch_page, fetch_pages
from app.utils.logging import get_logger
from app.utils.structured_data import extract_all_structured_data

log = get_logger(__name__)


class ScraperAgent:
    """Execute a scraping plan and return raw extracted data."""

    async def scrape(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> list[PageData]:
        log.info("Starting scrape for %s (js=%s, max_items=%s, method=%s)", plan.url, plan.requires_javascript, max_items, extraction_method)

        if plan.pagination == PaginationStrategy.API_ENDPOINT and plan.api_endpoint:
            pages = await self._scrape_api(plan, max_items=max_items)
        elif plan.requires_javascript:
            pages = await self._scrape_js(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback)
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
                pages = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback)
        else:
            pages = await self._scrape_static(plan, max_items=max_items, extraction_method=extraction_method, progress_callback=progress_callback)

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

        return pages

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
            pages = await self._enrich_detail_pages(pages, plan, max_details=1)
            pages = await self._enrich_detail_api(pages, plan, max_details=1)

        return pages

    async def scrape_preview_dual(self, plan: ScrapingPlan) -> tuple[list[PageData], list[PageData]]:
        """Fetch the *landing page only* and return two 1-item previews.

        Returns (css_pages, smart_pages).  Each contains at most 1 item.
        This is intentionally lightweight — no pagination, no tab clicking,
        no scrolling — just the first page load and a single sample item
        from each extraction method.
        """
        log.info("Dual preview scrape for %s", plan.url)

        # ── Fetch the landing page once (no pagination / tabs) ──────────
        used_js = plan.requires_javascript
        if plan.requires_javascript:
            async with get_browser() as browser:
                html = await fetch_page_js(browser, plan.url, wait_selector=plan.wait_selector)
        else:
            html = await fetch_page(plan.url)

        if not html:
            return [], []

        # If CSS found 0 items and we used httpx, retry with Playwright
        preview_html = self._slice_html_for_preview(html, plan.target.item_container_selector)
        css_items = self._extract_items(preview_html, plan.target, plan.detail_api_plan)[:1]

        if not css_items and not used_js:
            log.warning("CSS found 0 items with static fetch — retrying with Playwright for %s", plan.url)
            async with get_browser() as browser:
                html = await fetch_page_js(browser, plan.url, wait_selector=plan.wait_selector)
            if html:
                preview_html = self._slice_html_for_preview(html, plan.target.item_container_selector)
                css_items = self._extract_items(preview_html, plan.target, plan.detail_api_plan)[:1]

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

        log.info("Dual preview — CSS: %d items, Smart: %d items", len(css_items), len(smart_items))

        sd = extract_all_structured_data(html)
        css_pages = [PageData(url=plan.url, items=css_items, structured_data=sd)] if css_items else []
        smart_pages = [PageData(url=plan.url, items=smart_items, structured_data=sd)] if smart_items else []

        # Enrich both with detail pages (1 item each)
        if css_pages and css_pages[0].items:
            css_pages = await self._enrich_detail_pages(css_pages, plan, max_details=1)
            css_pages = await self._enrich_detail_api(css_pages, plan, max_details=1)
        if smart_pages and smart_pages[0].items:
            smart_pages = await self._enrich_detail_pages(smart_pages, plan, max_details=1)
            smart_pages = await self._enrich_detail_api(smart_pages, plan, max_details=1)

        return css_pages, smart_pages

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
    ) -> list[PageData]:
        urls = self._resolve_page_urls(plan)
        log.info("Static scrape: %d page URL(s)", len(urls))
        html_map = await fetch_pages(urls)

        pages: list[PageData] = []
        total_items = 0
        for url, html in html_map.items():
            if max_items is not None and total_items >= max_items:
                break
            if not html:
                continue
            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
            if max_items is not None:
                items = items[: max_items - total_items]
            total_items += len(items)
            pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))

        # Detail page enrichment
        pages = await self._enrich_detail_pages(pages, plan, max_details=max_items, progress_callback=progress_callback)
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
        return pages

    # ------------------------------------------------------------------
    # JS-rendered (Playwright) path
    # ------------------------------------------------------------------
    async def _scrape_js(
        self, plan: ScrapingPlan, *, max_items: int | None = None,
        extraction_method: ExtractionMethod | None = None,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> list[PageData]:
        pages: list[PageData] = []
        total_items = 0

        def _limit_reached() -> bool:
            return max_items is not None and total_items >= max_items

        async with get_browser() as browser:
            match plan.pagination:
                case PaginationStrategy.NONE:
                    # Single page — scroll to bottom to reveal all items
                    page = await browser.new_page()
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)
                        log.info("Single page mode — scrolling to reveal all items")
                        await scroll_to_bottom(page, max_scrolls=settings.max_pages_per_crawl)
                        html = await page.content()
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[:max_items]
                        total_items += len(items)
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
                        if _limit_reached():
                            break
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[: max_items - total_items]
                        total_items += len(items)
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))

                case PaginationStrategy.INFINITE_SCROLL:
                    page = await browser.new_page()
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
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))
                    finally:
                        await page.close()

                case PaginationStrategy.LOAD_MORE_BUTTON if plan.pagination_selector:
                    page = await browser.new_page()
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
                        pages.append(PageData(url=plan.url, items=items, structured_data=extract_all_structured_data(html)))
                    finally:
                        await page.close()

                case PaginationStrategy.NEXT_BUTTON if plan.pagination_selector:
                    page = await browser.new_page()
                    try:
                        await page.goto(plan.url, wait_until="commit", timeout=120_000)
                        if plan.wait_selector:
                            try:
                                await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                            except Exception:
                                log.warning("wait_for_selector timed out for '%s' — continuing", plan.wait_selector)

                        for _ in range(settings.max_pages_per_crawl):
                            if _limit_reached():
                                break
                            html = await page.content()
                            items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                            if max_items is not None:
                                items = items[: max_items - total_items]
                            total_items += len(items)
                            pages.append(PageData(url=page.url, items=items, structured_data=extract_all_structured_data(html)))
                            if _limit_reached():
                                break
                            btn = await page.query_selector(plan.pagination_selector)
                            if not btn or not await btn.is_visible():
                                break
                            await btn.click()
                            await asyncio.sleep(1.5)
                            if plan.wait_selector:
                                await page.wait_for_selector(plan.wait_selector, timeout=10_000)
                    finally:
                        await page.close()

                case PaginationStrategy.PAGE_NUMBERS if plan.pagination_selector:
                    # Page-number pagination: use pre-resolved URLs first,
                    # then fall back to clicking next-page button to discover
                    # any pages the LLM missed.
                    urls = self._resolve_page_urls(plan)
                    page_obj = await browser.new_page()
                    try:
                        # Phase 1: visit each pre-resolved URL
                        for idx, url in enumerate(urls[: settings.max_pages_per_crawl]):
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
                            pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))
                            await asyncio.sleep(settings.request_delay_ms / 1000)

                        # Phase 2: if we probably have more pages (based on
                        # total_items_hint), keep clicking the next/pagination
                        # button to discover remaining pages.
                        expected = plan.total_items_hint or 0
                        items_per_page = (total_items // max(len(urls), 1)) if total_items else 100
                        expected_pages = (expected // max(items_per_page, 1)) + 1 if expected else 0
                        pages_visited = len(urls)

                        if total_items < expected and pages_visited < expected_pages:
                            log.info(
                                "Pre-resolved URLs yielded %d items but total_items_hint=%d — "
                                "clicking next-page button to discover remaining pages",
                                total_items, expected,
                            )
                            for _ in range(settings.max_pages_per_crawl - pages_visited):
                                if _limit_reached():
                                    break
                                btn = await page_obj.query_selector(plan.pagination_selector)
                                if not btn or not await btn.is_visible():
                                    log.info("No more next-page buttons found — pagination complete")
                                    break
                                await btn.click()
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
                        if _limit_reached():
                            break
                        html = await fetch_page_js(
                            browser, url, wait_selector=plan.wait_selector
                        )
                        items = await self._extract_items_with_fallback(html, plan.target, plan.detail_api_plan, extraction_method=extraction_method)
                        if max_items is not None:
                            items = items[: max_items - total_items]
                        total_items += len(items)
                        pages.append(PageData(url=url, items=items, structured_data=extract_all_structured_data(html)))
                        await asyncio.sleep(settings.request_delay_ms / 1000)

        # Detail page enrichment (JS path — use Playwright for detail pages too)
        pages = await self._enrich_detail_pages(pages, plan, max_details=max_items, progress_callback=progress_callback)
        pages = await self._enrich_detail_api(pages, plan, max_details=max_items)
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
    ) -> list[PageData]:
        """Collect detail-page URLs from items and fetch them.

        Works for both static and JS paths.  Uses the ``detail_link_selector``
        from the plan to find links within each item, or falls back to any
        field whose value looks like a relative/absolute URL with 'detail' in it.
        """
        if not plan.target.detail_link_selector:
            log.debug("No detail_link_selector in plan — skipping detail enrichment")
            return pages

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
                    item["detail_link"] = link
                    all_detail_urls.append(link)

        if not all_detail_urls:
            log.debug(
                "No detail URLs found in %d items (fields: %s)",
                sum(len(pd.items) for pd in pages),
                list(pages[0].items[0].keys()) if pages and pages[0].items else "empty",
            )
            return pages

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

        # Fetch detail pages
        detail_htmls: dict[str, str] = {}
        if plan.requires_javascript:
            async with get_browser() as browser:
                for idx, url in enumerate(unique_urls, 1):
                    log.info("Detail page %d/%d: %s", idx, total_detail, url)
                    if progress_callback:
                        progress_callback({
                            "stage": "enriching_details",
                            "detail_current": idx,
                            "detail_total": total_detail,
                            "detail_url": url,
                        })
                    try:
                        detail_htmls[url] = await asyncio.wait_for(
                            fetch_page_js(browser, url),
                            timeout=DETAIL_PAGE_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        log.warning(
                            "Detail page %d/%d timed out after %ds: %s",
                            idx, total_detail, DETAIL_PAGE_TIMEOUT_S, url,
                        )
                    except Exception as exc:
                        log.warning("Failed detail page %d/%d %s: %s", idx, total_detail, url, exc)
                    await asyncio.sleep(settings.request_delay_ms / 1000)
            log.info("Detail enrichment complete: fetched %d/%d pages", len(detail_htmls), total_detail)
        else:
            if progress_callback:
                progress_callback({
                    "stage": "enriching_details",
                    "detail_current": 0,
                    "detail_total": total_detail,
                    "detail_url": "(batch fetch)",
                })
            detail_htmls = await fetch_pages(unique_urls)
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

        return pages

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
            page = await browser.new_page()
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
                last_page_num = None
                for key in qs_first:
                    if key in qs_last:
                        try:
                            v_first = int(qs_first[key][0])
                            v_last = int(qs_last[key][0])
                            if v_last > v_first:
                                page_param = key
                                last_page_num = v_last
                                break
                        except (ValueError, IndexError):
                            continue

                if page_param and last_page_num is not None:
                    # Estimate items per page from hint - assume ~100 items per page
                    items_per_page = max(plan.total_items_hint // max(len(resolved), 1), 50)
                    estimated_pages = (plan.total_items_hint // items_per_page) + 1
                    if estimated_pages > len(resolved):
                        log.info(
                            "Auto-extrapolating pagination: LLM gave %d URLs but "
                            "total_items_hint=%d suggests ~%d pages — extending to page=%d",
                            len(resolved), plan.total_items_hint,
                            estimated_pages, estimated_pages,
                        )
                        base_parsed = urlparse(resolved[-1])
                        base_qs = parse_qs(base_parsed.query, keep_blank_values=True)
                        for page_num in range(last_page_num + 1, estimated_pages + 1):
                            new_qs = {k: v[0] for k, v in base_qs.items()}
                            new_qs[page_param] = str(page_num)
                            new_url = urlunparse((
                                base_parsed.scheme, base_parsed.netloc,
                                base_parsed.path, base_parsed.params,
                                urlencode(new_qs), base_parsed.fragment,
                            ))
                            resolved.append(new_url)

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

            items.append(record)
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
        - SMART_SCRAPER: SmartScraperGraph primary, CSS fallback on failure
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
                smart_items = await smart_extract_items(html, fields)
                if smart_items:
                    if target.detail_link_selector:
                        self._backfill_detail_links(html, smart_items, target)
                    log.info("SmartScraperGraph extraction: %d items", len(smart_items))
                    return smart_items
                log.warning("SmartScraperGraph returned 0 items — falling back to CSS")
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
            smart_items = await smart_extract_items(html, fields)
            if smart_items:
                if len(css_items) > len(smart_items):
                    log.warning(
                        "SmartScraperGraph extracted %d items but CSS selectors found %d — using CSS results",
                        len(smart_items), len(css_items),
                    )
                    return css_items
                log.info("SmartScraperGraph extracted %d items (fallback, CSS had %d)", len(smart_items), len(css_items))
                if target.detail_link_selector:
                    self._backfill_detail_links(html, smart_items, target)
                return smart_items
            log.warning(
                "SmartScraperGraph returned 0 items — using CSS results (%d items)", len(css_items)
            )

        return css_items

    def _backfill_detail_links(
        self,
        html: str,
        items: list[dict[str, str | None]],
        target: ScrapingTarget,
    ) -> None:
        """Ensure every item has a detail_link by falling back to CSS selectors."""
        # Check if any items are missing detail_link
        missing = [i for i, item in enumerate(items) if not item.get("detail_link")]
        if not missing:
            return

        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(target.item_container_selector)

        for idx in missing:
            if idx < len(containers):
                link_el = containers[idx].select_one(target.detail_link_selector)
                if link_el:
                    items[idx]["detail_link"] = link_el.get("href")
                    log.debug("Backfilled detail_link for item %d from CSS", idx)
