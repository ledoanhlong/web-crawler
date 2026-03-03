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


async def _dismiss_consent_overlays(page: Page) -> None:
    """Dismiss cookie consent / overlay banners that may block clicks.

    Handles both regular DOM consent banners and shadow DOM banners
    (e.g. Usercentrics, OneTrust) that hide their buttons inside a shadow root.
    """
    # 1. Try shadow DOM consent managers (Usercentrics, etc.)
    try:
        dismissed = await page.evaluate("""() => {
            // Usercentrics shadow DOM
            const uc = document.getElementById("usercentrics-root");
            if (uc && uc.shadowRoot) {
                const btns = uc.shadowRoot.querySelectorAll("button");
                for (const btn of btns) {
                    const text = (btn.textContent || "").toLowerCase();
                    if (text.includes("accept") || text.includes("agree") ||
                        text.includes("allow") || text.includes("ok") ||
                        text.includes("consent")) {
                        btn.click();
                        return "usercentrics";
                    }
                }
                // Fallback: click the last button (often "Accept All")
                if (btns.length > 0) { btns[btns.length - 1].click(); return "usercentrics-fallback"; }
            }
            return null;
        }""")
        if dismissed:
            log.debug("Dismissed shadow DOM consent overlay: %s", dismissed)
            await asyncio.sleep(1)
            return
    except Exception:
        pass

    # 2. Try regular DOM consent selectors
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
                return
        except Exception:
            pass


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
        await page.goto(url, wait_until="commit", timeout=120_000)
        # After commit, wait for the page to finish loading its resources
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass  # best-effort — continue with whatever loaded
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



# URL fragments that indicate noise / third-party widgets (not detail APIs)
_NOISE_URL_FRAGMENTS = {
    "config.json", "layout", "/chat", "ichat", "analytics", "tracking",
    "pixel", "beacon", "metrics", "/ads/", "consent", "cookie", "gdpr",
    "fonts", "icons", "socket.io", "heartbeat", "cdn-cgi", "recaptcha",
    "gtag", "gtm", "hotjar", "sentry", "newrelic", "datadog",
}

# Keys in JSON response that suggest seller/company detail data
_DETAIL_DATA_KEYS = {
    "name", "address", "phone", "email", "website", "description",
    "company", "contact", "fax", "city", "country", "postal", "zip",
    "street", "url", "profile", "social", "category", "product",
    "brand", "booth", "stand", "hall", "seller", "vendor", "exhibitor",
    "telephone", "store", "rating", "marketplace",
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

    # Count how many keys look like seller/company detail data
    detail_hits = sum(
        1 for k in all_keys if any(dk in k for dk in _DETAIL_DATA_KEYS)
    )

    # Bonus for URL patterns that suggest detail/profile endpoints
    url_bonus = 0
    for kw in ("seller", "vendor", "profile", "detail", "company", "exhibitor", "store"):
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
    detail API (filtering out noise like chat widgets, analytics, etc.).

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
        await page.goto(url, wait_until="commit", timeout=120_000)
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
        await _dismiss_consent_overlays(page)

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
    max_items: int | None = None,
    inner_pagination_selector: str | None = None,
    pagination_urls: list[str] | None = None,
) -> list[str]:
    """Click each tab matching *tab_selector* and return the resulting HTML for each.

    Handles compound pagination automatically:
    1. If ``pagination_urls`` are provided by the planner, use those directly.
    2. If ``inner_pagination_selector`` is set, detect numbered pages within each tab.
    3. Otherwise, auto-detect inner pagination from common CSS patterns.
    4. If no inner pagination is found, just click tabs and capture one page each.
    """
    page = await browser.new_page()
    htmls: list[str] = []
    try:
        await page.goto(url, wait_until="commit", timeout=120_000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass

        # --- Strategy 1: Pre-computed pagination URLs from the planner ---
        if pagination_urls:
            log.info("Using %d pre-computed pagination URLs from plan", len(pagination_urls))
            return await _navigate_page_urls(
                page, pagination_urls, wait_selector=wait_selector, max_items=max_items,
            )

        # --- Strategy 2: Detect inner numbered pagination ---
        inner_urls = await _extract_inner_pagination_urls(
            page, url, selector_hint=inner_pagination_selector,
        )
        if inner_urls:
            log.info(
                "Detected %d inner pagination pages — using numbered pagination instead of tabs",
                len(inner_urls),
            )
            return await _navigate_page_urls(
                page, inner_urls, wait_selector=wait_selector, max_items=max_items,
            )

        # --- Strategy 3: Pure alphabet tabs (no inner pagination) ---
        tabs = await page.query_selector_all(tab_selector)
        log.info("Found %d tabs with selector '%s'", len(tabs), tab_selector)
        for i, tab in enumerate(tabs):
            if max_items is not None and len(htmls) * 20 >= max_items:
                break
            try:
                await tab.scroll_into_view_if_needed(timeout=5_000)
                await tab.click(timeout=10_000)
            except Exception:
                log.debug("Tab %d not interactable via Playwright, using JS click", i)
                try:
                    await tab.evaluate("el => el.click()")
                except Exception as exc:
                    exc_str = str(exc)
                    if "Execution context was destroyed" in exc_str or "navigation" in exc_str.lower():
                        log.warning("Tab %d triggered navigation, reloading page", i)
                        try:
                            await page.goto(url, wait_until="commit", timeout=120_000)
                            tabs = await page.query_selector_all(tab_selector)
                            if i < len(tabs):
                                await tabs[i].evaluate("el => el.click()")
                            else:
                                log.warning("Tab %d no longer exists after reload, skipping", i)
                                continue
                        except Exception as reload_exc:
                            log.warning("Skipping tab %d — reload+click failed: %s", i, reload_exc)
                            continue
                    else:
                        log.warning("Skipping tab %d — JS click also failed: %s", i, exc)
                        continue
            await asyncio.sleep(1.5)
            if page.url != url and not page.url.startswith(url.split("?")[0]):
                log.warning("Tab %d caused navigation to %s, navigating back", i, page.url)
                await page.goto(url, wait_until="commit", timeout=120_000)
                tabs = await page.query_selector_all(tab_selector)
                continue
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10_000)
                except Exception:
                    pass
            htmls.append(await page.content())
    finally:
        await page.close()
    return htmls


async def _navigate_page_urls(
    page: Page,
    urls: list[str],
    *,
    wait_selector: str | None = None,
    max_items: int | None = None,
    items_per_page: int = 20,
) -> list[str]:
    """Navigate through a list of page URLs and collect HTML from each."""
    htmls: list[str] = []

    # First page may already be loaded — capture it, then navigate the rest
    if wait_selector:
        try:
            await page.wait_for_selector(wait_selector, timeout=10_000)
        except Exception:
            pass
    htmls.append(await page.content())

    for pg_url in urls[1:]:
        if max_items is not None and len(htmls) * items_per_page >= max_items:
            break
        try:
            await page.goto(pg_url, wait_until="commit", timeout=120_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass
            if wait_selector:
                try:
                    await page.wait_for_selector(wait_selector, timeout=10_000)
                except Exception:
                    pass
            htmls.append(await page.content())
            await asyncio.sleep(1)
        except Exception as exc:
            log.warning("Failed to load pagination page %s: %s", pg_url, exc)

    log.info("Collected HTML from %d pages", len(htmls))
    return htmls


async def _extract_inner_pagination_urls(
    page: Page,
    base_url: str,
    *,
    selector_hint: str | None = None,
) -> list[str]:
    """Detect numbered pagination links on the current page and return all page URLs.

    Uses ``selector_hint`` from the scraping plan if available, otherwise
    tries common pagination CSS patterns dynamically.
    """
    urls: list[str] = []
    try:
        # Build the JS query to find pagination links
        # If we have a hint from the planner, use it; otherwise try common selectors
        pager_links = await page.evaluate("""(selectorHint) => {
            // Try planner-provided selector first, then common patterns
            const selectors = [];
            if (selectorHint) selectors.push(selectorHint);
            selectors.push(
                '.pagination .pager a[href]',
                '.pagination a[href]',
                'nav.pagination a[href]',
                '.pager a[href]',
                '.page-numbers a[href]',
                '[class*="paginator"] a[href]',
                '[class*="paging"] a[href]',
                'ul.pagination a[href]',
            );

            for (const sel of selectors) {
                try {
                    const links = document.querySelectorAll(sel);
                    if (links.length < 2) continue;

                    const hrefs = [];
                    for (const a of links) {
                        const text = (a.textContent || '').trim();
                        // Only numbered page links (not "next", "prev", arrows, etc.)
                        if (/^\\d+$/.test(text)) {
                            hrefs.push({num: parseInt(text), href: a.href});
                        }
                    }
                    if (hrefs.length >= 2) return hrefs;
                } catch (e) { /* skip invalid selector */ }
            }
            return [];
        }""", selector_hint)

        if len(pager_links) < 2:
            return []

        # Sort by page number and deduplicate
        seen: set[str] = set()
        sorted_links = sorted(pager_links, key=lambda x: x["num"])
        for link in sorted_links:
            href = link["href"]
            if href not in seen:
                seen.add(href)
                urls.append(href)

        # If we only have a subset of pages (e.g. pages 1-10 out of 80),
        # try to extrapolate the full list by detecting the URL pattern
        max_page = max(link["num"] for link in sorted_links)
        if max_page > len(urls):
            urls = _extrapolate_page_urls(urls, sorted_links, max_page)

        log.info("Found %d numbered pagination pages (max page number: %d)", len(urls), max_page)
    except Exception as exc:
        log.debug("Inner pagination detection failed: %s", exc)

    return urls


def _extrapolate_page_urls(
    existing_urls: list[str],
    sorted_links: list[dict],
    max_page: int,
) -> list[str]:
    """Try to generate URLs for pages not directly linked by detecting the URL pattern."""
    import re
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    if len(sorted_links) < 2:
        return existing_urls

    # Find the varying parameter between page 1 and page 2
    url1 = sorted_links[0]["href"]
    url2 = sorted_links[1]["href"]
    num1 = sorted_links[0]["num"]
    num2 = sorted_links[1]["num"]

    parsed1 = urlparse(url1)
    parsed2 = urlparse(url2)

    if parsed1.path != parsed2.path:
        return existing_urls  # path differs — too complex to extrapolate

    qs1 = parse_qs(parsed1.query, keep_blank_values=True)
    qs2 = parse_qs(parsed2.query, keep_blank_values=True)

    # Find the parameter that changed
    changing_param = None
    val1 = val2 = 0
    for key in qs1:
        if key in qs2 and qs1[key] != qs2[key]:
            try:
                v1 = int(qs1[key][0])
                v2 = int(qs2[key][0])
                changing_param = key
                val1, val2 = v1, v2
                break
            except ValueError:
                continue

    if not changing_param:
        return existing_urls

    # Calculate step size
    step = (val2 - val1) // (num2 - num1) if num2 != num1 else 0
    if step <= 0:
        return existing_urls

    start_val = val1 - (num1 - 1) * step  # value for page 1

    # Generate all page URLs
    all_urls: list[str] = []
    for pg in range(1, max_page + 1):
        qs_copy = {k: v[0] for k, v in qs1.items()}
        qs_copy[changing_param] = str(start_val + (pg - 1) * step)
        new_query = urlencode(qs_copy)
        new_url = urlunparse((
            parsed1.scheme, parsed1.netloc, parsed1.path,
            parsed1.params, new_query, parsed1.fragment,
        ))
        all_urls.append(new_url)

    log.info("Extrapolated %d page URLs from pattern (param=%s, step=%d)", len(all_urls), changing_param, step)
    return all_urls
