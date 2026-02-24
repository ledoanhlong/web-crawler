"""ScraperAgent — Executes a ScrapingPlan and returns raw PageData.

Supports:
- Static pages via httpx
- JS-rendered pages via Playwright
- Multiple pagination strategies (next button, page numbers, alphabet tabs,
  infinite scroll, load-more, and direct API endpoints)
- Optional detail-page enrichment
"""

from __future__ import annotations

import asyncio
import json

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
            detail_pages = await self._fetch_detail_pages(
                html, plan.target.detail_link_selector, url
            )
            pages.append(PageData(url=url, items=items, detail_pages=detail_pages))
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
                        await page.goto(plan.url, wait_until="networkidle", timeout=30_000)
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
                        await page.goto(plan.url, wait_until="networkidle", timeout=30_000)
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
                        await page.goto(plan.url, wait_until="networkidle", timeout=30_000)
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
        if plan.target.detail_link_selector and plan.detail_page_fields:
            pages = await self._enrich_detail_pages_js(pages, plan)

        return pages

    # ------------------------------------------------------------------
    # API endpoint path
    # ------------------------------------------------------------------
    async def _scrape_api(self, plan: ScrapingPlan) -> list[PageData]:
        """Fetch data directly from a discovered JSON API."""
        log.info("Scraping API endpoint: %s", plan.api_endpoint)
        pages: list[PageData] = []
        page_num = 0
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
                pages.append(PageData(url=f"{plan.api_endpoint}?page={page_num}", items=items))
                page_num += 1
                await asyncio.sleep(settings.request_delay_ms / 1000)

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
            items.append(record)
        return items

    async def _fetch_detail_pages(
        self,
        listing_html: str,
        detail_link_selector: str | None,
        base_url: str,
    ) -> dict[str, str]:
        """For static pages, fetch detail page HTML for enrichment."""
        if not detail_link_selector:
            return {}
        soup = BeautifulSoup(listing_html, "lxml")
        links = soup.select(detail_link_selector)
        urls: list[str] = []
        for a in links:
            href = a.get("href")
            if href:
                # Resolve relative URLs
                if href.startswith("/"):
                    from urllib.parse import urlparse

                    parsed = urlparse(base_url)
                    href = f"{parsed.scheme}://{parsed.netloc}{href}"
                urls.append(href)
        if not urls:
            return {}
        log.info("Fetching %d detail pages", len(urls))
        return await fetch_pages(urls)

    async def _enrich_detail_pages_js(
        self, pages: list[PageData], plan: ScrapingPlan
    ) -> list[PageData]:
        """For JS-rendered sites, visit each detail page with Playwright."""
        all_detail_urls: set[str] = set()
        for pd in pages:
            for item in pd.items:
                link = item.get("detail_link")
                if link:
                    all_detail_urls.add(link)

        if not all_detail_urls:
            return pages

        log.info("Enriching %d detail pages via Playwright", len(all_detail_urls))
        detail_htmls: dict[str, str] = {}
        async with get_browser() as browser:
            for url in all_detail_urls:
                try:
                    detail_htmls[url] = await fetch_page_js(browser, url)
                except Exception as exc:
                    log.warning("Failed detail page %s: %s", url, exc)
                await asyncio.sleep(settings.request_delay_ms / 1000)

        for pd in pages:
            pd.detail_pages = detail_htmls
        return pages
