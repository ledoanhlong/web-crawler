"""ScraperAgent — Executes a ScrapingPlan and returns raw PageData.

Supports:
- Static pages via httpx
- JS-rendered pages via Playwright
- Multiple pagination strategies (next button, page numbers, alphabet tabs,
  infinite scroll, load-more, and direct API endpoints)
- Optional detail-page enrichment
- Preview mode: scrape a single item (with its detail page) for user validation
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models.schemas import PageData, PaginationStrategy, ScrapingPlan, ScrapingTarget
from app.utils.browser import (
    click_all_tabs,
    click_load_more,
    fetch_page_js,
    get_browser,
    scroll_to_bottom,
)
from app.utils.http import fetch_page, fetch_pages
from app.utils.logging import get_logger

log = get_logger(__name__)


class ScraperAgent:
    """Execute a scraping plan and return raw extracted data."""

    async def scrape(self, plan: ScrapingPlan) -> list[PageData]:
        log.info("Starting scrape for %s (js=%s)", plan.url, plan.requires_javascript)

        if plan.pagination == PaginationStrategy.API_ENDPOINT and plan.api_endpoint:
            return await self._scrape_api(plan)

        if plan.requires_javascript:
            return await self._scrape_js(plan)

        return await self._scrape_static(plan)

    async def scrape_preview(self, plan: ScrapingPlan) -> list[PageData]:
        """Scrape just the first item (with its detail page) for preview."""
        log.info("Preview scrape for %s", plan.url)

        if plan.pagination == PaginationStrategy.API_ENDPOINT and plan.api_endpoint:
            pages = await self._scrape_api(plan, max_items=1)
        elif plan.requires_javascript:
            pages = await self._scrape_preview_js(plan)
        else:
            pages = await self._scrape_preview_static(plan)

        # Enrich the single preview item with its detail page
        if pages and pages[0].items:
            pages = await self._enrich_detail_pages(pages, plan, max_details=1)

        return pages

    # ------------------------------------------------------------------
    # Preview helpers
    # ------------------------------------------------------------------
    async def _scrape_preview_static(self, plan: ScrapingPlan) -> list[PageData]:
        """Fetch the first page and extract only the first item."""
        html = await fetch_page(plan.url)
        if not html:
            return []
        items = self._extract_items(html, plan.target)
        if items:
            items = items[:1]
        return [PageData(url=plan.url, items=items)]

    async def _scrape_preview_js(self, plan: ScrapingPlan) -> list[PageData]:
        """Use Playwright to fetch the first page and extract only the first item."""
        async with get_browser() as browser:
            html = await fetch_page_js(browser, plan.url, wait_selector=plan.wait_selector)
            items = self._extract_items(html, plan.target)
            if items:
                items = items[:1]
            return [PageData(url=plan.url, items=items)]

    # ------------------------------------------------------------------
    # Static (httpx) path
    # ------------------------------------------------------------------
    async def _scrape_static(self, plan: ScrapingPlan) -> list[PageData]:
        urls = self._resolve_page_urls(plan)
        log.info("Static scrape: %d page URL(s)", len(urls))
        html_map = await fetch_pages(urls)

        pages: list[PageData] = []
        for url, html in html_map.items():
            if not html:
                continue
            items = self._extract_items(html, plan.target)
            pages.append(PageData(url=url, items=items))

        # Detail page enrichment
        pages = await self._enrich_detail_pages(pages, plan)
        return pages

    # ------------------------------------------------------------------
    # JS-rendered (Playwright) path
    # ------------------------------------------------------------------
    async def _scrape_js(self, plan: ScrapingPlan) -> list[PageData]:
        pages: list[PageData] = []
        async with get_browser() as browser:
            match plan.pagination:
                case PaginationStrategy.ALPHABET_TABS if plan.alphabet_tab_selector:
                    htmls = await click_all_tabs(
                        browser,
                        plan.url,
                        plan.alphabet_tab_selector,
                        wait_selector=plan.wait_selector,
                    )
                    for html in htmls:
                        items = self._extract_items(html, plan.target)
                        pages.append(PageData(url=plan.url, items=items))

                case PaginationStrategy.INFINITE_SCROLL:
                    page = await browser.new_page()
                    try:
                        await page.goto(plan.url, wait_until="domcontentloaded", timeout=60_000)
                        if plan.wait_selector:
                            await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                        await scroll_to_bottom(page, max_scrolls=settings.max_pages_per_crawl)
                        html = await page.content()
                        items = self._extract_items(html, plan.target)
                        pages.append(PageData(url=plan.url, items=items))
                    finally:
                        await page.close()

                case PaginationStrategy.LOAD_MORE_BUTTON if plan.pagination_selector:
                    page = await browser.new_page()
                    try:
                        await page.goto(plan.url, wait_until="domcontentloaded", timeout=60_000)
                        if plan.wait_selector:
                            await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                        await click_load_more(page, plan.pagination_selector)
                        html = await page.content()
                        items = self._extract_items(html, plan.target)
                        pages.append(PageData(url=plan.url, items=items))
                    finally:
                        await page.close()

                case PaginationStrategy.NEXT_BUTTON if plan.pagination_selector:
                    page = await browser.new_page()
                    try:
                        await page.goto(plan.url, wait_until="domcontentloaded", timeout=60_000)
                        if plan.wait_selector:
                            await page.wait_for_selector(plan.wait_selector, timeout=15_000)
                        for _ in range(settings.max_pages_per_crawl):
                            html = await page.content()
                            items = self._extract_items(html, plan.target)
                            pages.append(PageData(url=page.url, items=items))
                            btn = await page.query_selector(plan.pagination_selector)
                            if not btn or not await btn.is_visible():
                                break
                            await btn.click()
                            await asyncio.sleep(1.5)
                            if plan.wait_selector:
                                await page.wait_for_selector(plan.wait_selector, timeout=10_000)
                    finally:
                        await page.close()

                case _:
                    # Single page or page-number pagination with pre-resolved URLs
                    urls = self._resolve_page_urls(plan)
                    for url in urls[: settings.max_pages_per_crawl]:
                        html = await fetch_page_js(
                            browser, url, wait_selector=plan.wait_selector
                        )
                        items = self._extract_items(html, plan.target)
                        pages.append(PageData(url=url, items=items))
                        await asyncio.sleep(settings.request_delay_ms / 1000)

        # Detail page enrichment (JS path — use Playwright for detail pages too)
        pages = await self._enrich_detail_pages(pages, plan)
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
                params = {**plan.api_params, "page": str(page_num)}
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
                    # Try common wrapper keys
                    for key in ("data", "items", "results", "exhibitors", "records", "list"):
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
    ) -> list[PageData]:
        """Collect detail-page URLs from items and fetch them.

        Works for both static and JS paths.  Uses the ``detail_link_selector``
        from the plan to find links within each item, or falls back to any
        field whose value looks like a relative/absolute URL with 'detail' in it.
        """
        if not plan.target.detail_link_selector:
            return pages

        # Collect detail URLs from items
        all_detail_urls: list[str] = []
        base_parsed = urlparse(plan.url)
        base_origin = f"{base_parsed.scheme}://{base_parsed.netloc}"

        for pd in pages:
            for item in pd.items:
                link = item.get("detail_link")
                if link:
                    if link.startswith("/"):
                        link = base_origin + link
                    all_detail_urls.append(link)

        if not all_detail_urls:
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

        log.info("Enriching %d detail page(s)", len(unique_urls))

        # Fetch detail pages
        detail_htmls: dict[str, str] = {}
        if plan.requires_javascript:
            async with get_browser() as browser:
                for url in unique_urls:
                    try:
                        detail_htmls[url] = await fetch_page_js(browser, url)
                    except Exception as exc:
                        log.warning("Failed detail page %s: %s", url, exc)
                    await asyncio.sleep(settings.request_delay_ms / 1000)
        else:
            detail_htmls = await fetch_pages(unique_urls)

        for pd in pages:
            pd.detail_pages.update(detail_htmls)
        return pages

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_page_urls(self, plan: ScrapingPlan) -> list[str]:
        if plan.pagination_urls:
            return plan.pagination_urls[: settings.max_pages_per_crawl]
        return [plan.url]

    def _extract_items(self, html: str, target: ScrapingTarget) -> list[dict[str, str | None]]:
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
                el = container.select_one(selector)
                if el is None:
                    record[field] = None
                    continue
                attr = target.field_attributes.get(field)
                if attr:
                    record[field] = el.get(attr)  # type: ignore[assignment]
                else:
                    record[field] = el.get_text(separator=" ", strip=True)

            # Also extract detail link if selector is present
            if target.detail_link_selector and "detail_link" not in record:
                link_el = container.select_one(target.detail_link_selector)
                if link_el:
                    record["detail_link"] = link_el.get("href")  # type: ignore[assignment]

            items.append(record)
        return items
