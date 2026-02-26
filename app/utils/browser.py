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
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if wait_selector:
            log.debug("Waiting for selector: %s", wait_selector)
            await page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
        else:
            # Give JS-rendered content a moment to settle
            await asyncio.sleep(3)
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



# URL fragments that indicate noise / third-party widgets (not exhibitor detail APIs)
_NOISE_URL_FRAGMENTS = {
    "config.json", "layout", "/chat", "ichat", "analytics", "tracking",
    "pixel", "beacon", "metrics", "/ads/", "consent", "cookie", "gdpr",
    "fonts", "icons", "socket.io", "heartbeat", "cdn-cgi", "recaptcha",
    "gtag", "gtm", "hotjar", "sentry", "newrelic", "datadog",
}

# Keys in JSON response that suggest exhibitor/company detail data
_DETAIL_DATA_KEYS = {
    "name", "address", "phone", "email", "website", "description",
    "company", "contact", "fax", "city", "country", "postal", "zip",
    "street", "url", "profile", "social", "category", "product",
    "brand", "booth", "stand", "hall", "exhibitor", "telephone",
}


def _score_api_response(api_url: str, body: dict) -> int:
    """Score an API response on how likely it is to be the detail API.

    Higher is better; negative means noise/irrelevant.
    """
    url_lower = api_url.lower()

    # Penalize noise URLs heavily
    for frag in _NOISE_URL_FRAGMENTS:
        if frag in url_lower:
            return -1000

    # Collect all keys (including one level of nesting)
    all_keys: set[str] = set()
    for k, v in body.items():
        all_keys.add(k.lower())
        if isinstance(v, dict):
            all_keys.update(kk.lower() for kk in v.keys())

    # Count how many keys look like exhibitor data
    detail_hits = sum(
        1 for k in all_keys if any(dk in k for dk in _DETAIL_DATA_KEYS)
    )

    # Bonus for URL patterns that suggest detail/profile endpoints
    url_bonus = 0
    for kw in ("exhibitor", "profile", "detail", "company", "seller", "vendor"):
        if kw in url_lower:
            url_bonus += 15

    return detail_hits * 10 + url_bonus + len(body)


async def intercept_detail_api(
    browser: Browser,
    url: str,
    item_container_selector: str,
    detail_button_selector: str,
    *,
    wait_selector: str | None = None,
    timeout_ms: int = 15_000,
) -> tuple[str | None, dict | None]:
    """Click a JS-only detail button on the first listing item and capture the API call.

    Listens for all network responses that return JSON after clicking the detail
    button on the first item.  Scores each response to find the most likely
    exhibitor detail API (filtering out noise like chat widgets, analytics, etc.).

    Returns ``(api_url, response_json)`` or ``(None, None)`` if nothing captured.
    """
    page = await browser.new_page()
    captured: list[tuple[str, dict]] = []

    async def _on_response(response):  # type: ignore[no-untyped-def]
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return
        try:
            body = await response.json()
            if isinstance(body, dict):
                captured.append((response.url, body))
            elif isinstance(body, list) and body and isinstance(body[0], dict):
                # Wrap list responses so we can still process them
                captured.append((response.url, {"_items": body}))
        except Exception as exc:
            log.debug("Failed to parse JSON from %s: %s", response.url[:100], exc)

    try:
        page.on("response", _on_response)
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=15_000)
        else:
            await asyncio.sleep(3)

        # Find the first item container
        container = await page.query_selector(item_container_selector)
        if not container:
            log.warning("No item container found for selector: %s", item_container_selector)
            return None, None

        # Find the detail button within it
        button = await container.query_selector(detail_button_selector)
        if not button:
            log.warning("No detail button found for selector: %s", detail_button_selector)
            return None, None

        # Dismiss cookie consent / overlay banners that may block clicks
        for consent_sel in [
            "[class*='cookie'] [class*='accept']",
            "[class*='cookie'] [class*='agree']",
            "[class*='consent'] button",
            "#onetrust-accept-btn-handler",
            ".cc-accept",
            "[data-testid='uc-accept-all-button']",
            "button[id*='accept']",
        ]:
            try:
                consent_btn = await page.query_selector(consent_sel)
                if consent_btn and await consent_btn.is_visible():
                    await consent_btn.click()
                    log.debug("Dismissed consent overlay: %s", consent_sel)
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # Scroll button into view and let the page settle
        await button.scroll_into_view_if_needed()
        await asyncio.sleep(0.5)

        # Clear captures from page load — only keep post-click responses
        captured.clear()

        log.info("Clicking detail button to intercept API call...")
        # Use JS click to bypass any remaining overlays (most reliable for SPAs)
        await button.evaluate("el => el.click()")

        # Wait for network activity to settle, then collect responses
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass  # Timeout is fine — some pages keep connections open
        # Extra buffer for late API calls
        await asyncio.sleep(2)

        log.debug(
            "Captured %d JSON responses after click: %s",
            len(captured),
            [u[:100] for u, _ in captured],
        )

        if not captured:
            log.warning("No JSON API response captured after clicking detail button")
            return None, None

        # Score all captured responses and pick the best
        scored = [(u, b, _score_api_response(u, b)) for u, b in captured]
        scored.sort(key=lambda x: x[2], reverse=True)

        for u, _, s in scored:
            log.debug("  API candidate: score=%d url=%s", s, u[:120])

        best_url, best_body, best_score = scored[0]

        if best_score < 0:
            log.warning(
                "All %d captured APIs look like noise (best score=%d: %s)",
                len(scored), best_score, best_url[:120],
            )
            return None, None

        log.info(
            "Captured detail API: %s (score=%d, %d keys)",
            best_url, best_score, len(best_body),
        )
        return best_url, best_body
    finally:
        await page.close()


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
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
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
