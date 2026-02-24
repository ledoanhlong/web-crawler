"""Playwright browser helpers for JS-heavy pages."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from playwright.async_api import Browser, Page, async_playwright

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)


@asynccontextmanager
async def get_browser() -> AsyncIterator[Browser]:
    """Yield a Playwright Chromium browser instance.

    Supports both local launch and remote connection via
    ``PLAYWRIGHT_WS_ENDPOINT`` for cloud deployments (e.g. Browserless).
    """
    pw = await async_playwright().start()
    try:
        if settings.playwright_ws_endpoint:
            log.info("Connecting to remote browser: %s", settings.playwright_ws_endpoint)
            browser = await pw.chromium.connect(settings.playwright_ws_endpoint)
        else:
            log.info("Launching local Chromium (headless=%s)", settings.playwright_headless)
            browser = await pw.chromium.launch(headless=settings.playwright_headless)
        yield browser
        await browser.close()
    finally:
        await pw.stop()


async def fetch_page_js(
    browser: Browser,
    url: str,
    *,
    wait_selector: str | None = None,
    wait_timeout_ms: int = 15_000,
) -> str:
    """Navigate to a URL in a new page, wait for content, and return the HTML."""
    page: Page = await browser.new_page()
    try:
        log.info("Playwright GET %s", url)
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        if wait_selector:
            log.debug("Waiting for selector: %s", wait_selector)
            await page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
        else:
            # Give JS-rendered content a moment to settle
            await asyncio.sleep(2)
        return await page.content()
    finally:
        await page.close()


async def scroll_to_bottom(page: Page, *, pause_ms: int = 1000, max_scrolls: int = 50) -> None:
    """Scroll the page to the bottom to trigger infinite-scroll loading."""
    prev_height = 0
    for _ in range(max_scrolls):
        curr_height = await page.evaluate("document.body.scrollHeight")
        if curr_height == prev_height:
            break
        prev_height = curr_height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(pause_ms / 1000)


async def click_load_more(
    page: Page,
    selector: str,
    *,
    max_clicks: int = 100,
    pause_ms: int = 1500,
) -> None:
    """Repeatedly click a 'Load More' button until it disappears or limit is reached."""
    for i in range(max_clicks):
        btn = await page.query_selector(selector)
        if not btn or not await btn.is_visible():
            log.debug("Load-more button gone after %d clicks", i)
            break
        await btn.click()
        await asyncio.sleep(pause_ms / 1000)


async def click_all_tabs(
    browser: Browser,
    url: str,
    tab_selector: str,
    *,
    wait_selector: str | None = None,
) -> list[str]:
    """Click each tab matching *tab_selector* and return the resulting HTML for each."""
    page = await browser.new_page()
    htmls: list[str] = []
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        tabs = await page.query_selector_all(tab_selector)
        log.info("Found %d tabs with selector '%s'", len(tabs), tab_selector)
        for tab in tabs:
            await tab.click()
            await asyncio.sleep(1.5)
            if wait_selector:
                await page.wait_for_selector(wait_selector, timeout=10_000)
            htmls.append(await page.content())
    finally:
        await page.close()
    return htmls
