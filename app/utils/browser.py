"""Playwright browser helpers for JS-heavy pages."""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from playwright.async_api import Browser, Page, Route, async_playwright

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)


def _register_async_response_listener(
    page: Page,
    handler: Callable[[Any], Awaitable[None]],
) -> tuple[set[asyncio.Task[None]], Callable[[Any], None]]:
    """Bridge Playwright event callbacks to scheduled asyncio tasks.

    Playwright event emitters do not await async callbacks. Scheduling the work
    explicitly avoids lost captures and coroutine-never-awaited warnings.
    """
    pending: set[asyncio.Task[None]] = set()

    def _listener(response: Any) -> None:
        task = asyncio.create_task(handler(response))
        pending.add(task)

        def _cleanup(done: asyncio.Task[None]) -> None:
            pending.discard(done)
            try:
                done.result()
            except Exception as exc:
                log.debug("Response listener task failed: %s", exc)

        task.add_done_callback(_cleanup)

    maybe_awaitable = page.on("response", _listener)
    if inspect.isawaitable(maybe_awaitable):
        task = asyncio.create_task(maybe_awaitable)  # pragma: no cover - real Playwright is sync
        pending.add(task)

        def _cleanup_registration(done: asyncio.Task[None]) -> None:
            pending.discard(done)
            try:
                done.result()
            except Exception as exc:
                log.debug("Response listener registration failed: %s", exc)

        task.add_done_callback(_cleanup_registration)
    return pending, _listener


async def _drain_pending_tasks(pending: set[asyncio.Task[None]]) -> None:
    """Wait for currently scheduled response-handler tasks to finish."""
    if not pending:
        return
    await asyncio.gather(*list(pending), return_exceptions=True)


async def _await_if_needed(value: object) -> None:
    """Await a Playwright/mock return value when it is awaitable."""
    if inspect.isawaitable(value):
        await value


# ---------------------------------------------------------------------------
# Realistic user agent pool (rotated per page when stealth is enabled)
# ---------------------------------------------------------------------------
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
]

# Realistic viewport sizes (common desktop resolutions)
_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1680, "height": 1050},
    {"width": 1280, "height": 720},
    {"width": 1600, "height": 900},
]


# ---------------------------------------------------------------------------
# Stealth script — injected on every page to avoid bot detection
# ---------------------------------------------------------------------------
_STEALTH_JS = """
() => {
    // Hide webdriver flag
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    // Spoof plugins (realistic Chrome plugin list)
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const p = [
                {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
            ];
            p.length = 3;
            return p;
        },
    });
    // Spoof languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
    // Chrome runtime stub
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
    // Permissions query override
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(parameters);

    // WebGL vendor/renderer spoofing
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (Intel)';      // UNMASKED_VENDOR_WEBGL
        if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)'; // UNMASKED_RENDERER_WEBGL
        return getParameter.call(this, param);
    };
    // Also patch WebGL2
    if (typeof WebGL2RenderingContext !== 'undefined') {
        const getParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {
            if (param === 37445) return 'Google Inc. (Intel)';
            if (param === 37446) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
            return getParam2.call(this, param);
        };
    }

    // Connection API spoofing (headless often missing)
    if (!navigator.connection) {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({effectiveType: '4g', rtt: 50, downlink: 10, saveData: false}),
        });
    }

    // Spoof hardware concurrency (headless defaults vary)
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

    // Spoof deviceMemory
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

    // Prevent iframe detection of automation
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
            return window;
        }
    });
}
"""

# ---------------------------------------------------------------------------
# Layer 1 — Block consent management scripts from loading
# ---------------------------------------------------------------------------
_CONSENT_BLOCK_PATTERNS = [
    "*onetrust.com*",
    "*cookiebot.com*",
    "*cookielaw.org*",
    "*usercentrics.eu*",
    "*trustarc.com*",
    "*quantcast.com/choice*",
    "*privacy-center.org*",
    "*consentmanager.net*",
    "*osano.com*",
    "*iubenda.com/cookie*",
]

# ---------------------------------------------------------------------------
# Layer 2 — MutationObserver that kills consent overlays on DOM insertion
# ---------------------------------------------------------------------------
_CONSENT_KILLER_JS = """
() => {
    const CONSENT_SELS = [
        '#onetrust-consent-sdk',
        '#CybotCookiebotDialog',
        '#usercentrics-root',
        '[class*="cookie-banner"]',
        '[class*="consent-banner"]',
        '[id*="cookie-consent"]',
        '.cc-window',
        '#gdpr-consent-tool',
    ];
    function killOverlays() {
        for (const sel of CONSENT_SELS) {
            document.querySelectorAll(sel).forEach(function(el) {
                el.style.setProperty('display', 'none', 'important');
                el.style.setProperty('pointer-events', 'none', 'important');
                el.setAttribute('aria-hidden', 'true');
            });
        }
    }
    killOverlays();
    var obs = new MutationObserver(killOverlays);
    if (document.body) {
        obs.observe(document.body, { childList: true, subtree: true });
    } else {
        document.addEventListener('DOMContentLoaded', function() {
            killOverlays();
            obs.observe(document.body, { childList: true, subtree: true });
        });
    }
}
"""


# ---------------------------------------------------------------------------
# Page factory — creates a Playwright page with all protections enabled
# ---------------------------------------------------------------------------
async def create_page(browser: Browser) -> Page:
    """Create a new Playwright page with stealth, consent blocking, and overlay killing.

    Every page created through this factory automatically:
    1. Uses a randomized user agent and viewport (when enabled)
    2. Blocks known consent management script URLs (Layer 1)
    3. Injects a MutationObserver that hides overlay DOM on insertion (Layer 2)
    4. Applies stealth anti-detection scripts (WebGL, plugins, navigator fingerprint)
    """
    context_kwargs: dict[str, Any] = {}

    if settings.stealth_enabled:
        if settings.stealth_randomize_user_agent:
            context_kwargs["user_agent"] = random.choice(_USER_AGENTS)
        if settings.stealth_randomize_viewport:
            vp = random.choice(_VIEWPORTS)
            context_kwargs["viewport"] = vp
            context_kwargs["screen"] = vp

    # Create a browser context with stealth settings for better fingerprint
    if context_kwargs:
        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()
    else:
        page = await browser.new_page()

    # Layer 1: block consent scripts from loading
    async def _abort_route(route: Route) -> None:
        await route.abort()

    for pattern in _CONSENT_BLOCK_PATTERNS:
        await page.route(pattern, _abort_route)

    # Stealth
    if settings.stealth_enabled:
        try:
            await page.add_init_script(_STEALTH_JS)
        except Exception as exc:
            log.debug("Stealth injection failed (non-fatal): %s", exc)

    # Layer 2: kill overlays via MutationObserver
    try:
        await page.add_init_script(_CONSENT_KILLER_JS)
    except Exception as exc:
        log.debug("Consent killer injection failed (non-fatal): %s", exc)

    return page


# ---------------------------------------------------------------------------
# Safe click — click with automatic overlay recovery
# ---------------------------------------------------------------------------
async def safe_click(page: Page, selector: str, *, timeout: int = 10_000) -> bool:
    """Click an element, recovering from overlay interception.

    1. Try Playwright click (respects visibility, scrolling, etc.)
    2. On failure: dismiss overlays reactively, then retry with JS click
    Returns True if the click succeeded, False otherwise.
    """
    try:
        el = await page.query_selector(selector)
    except Exception:
        log.debug("Invalid selector for safe_click: %s", selector)
        return False
    if not el:
        return False
    try:
        if not await el.is_visible():
            return False
    except Exception:
        return False

    try:
        await el.click(timeout=timeout)
        return True
    except Exception as exc:
        log.debug("Playwright click failed on '%s': %s — trying recovery", selector, type(exc).__name__)

    # Layer 3 fallback: dismiss overlays reactively then JS click
    await _dismiss_consent_overlays(page)
    try:
        # Re-query in case DOM changed
        el = await page.query_selector(selector)
        if el:
            await el.evaluate("el => el.click()")
            return True
    except Exception as js_exc:
        log.warning("JS click also failed on '%s': %s", selector, js_exc)
    return False


# ---------------------------------------------------------------------------
# Layer 3 — Reactive consent dismissal (last-resort fallback)
# ---------------------------------------------------------------------------
async def _dismiss_consent_overlays(page: Page) -> None:
    """Dismiss cookie consent / overlay banners that may block clicks.

    This is the last-resort Layer 3 fallback.  Normally, Layers 1 and 2
    (route blocking + MutationObserver) prevent overlays from appearing.
    This function handles edge cases where those layers miss something.
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
                break
        except Exception:
            pass

    # 3. Force-remove known consent overlay containers
    try:
        removed = await page.evaluate("""() => {
            const sels = [
                '#onetrust-consent-sdk',
                '#CybotCookiebotDialog',
                '[class*="cookie-banner"]',
                '[class*="consent-banner"]',
                '[id*="cookie-consent"]',
            ];
            let removed = [];
            for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el) { el.remove(); removed.push(sel); }
            }
            return removed.length ? removed.join(', ') : null;
        }""")
        if removed:
            log.debug("Force-removed consent overlay: %s", removed)
    except Exception:
        pass


@asynccontextmanager
async def get_browser() -> AsyncIterator[Browser]:
    """Yield a local Playwright Chromium browser instance."""
    pw = await async_playwright().start()
    try:
        log.info("Launching local Chromium (headless=%s)", settings.playwright_headless)
        browser = await pw.chromium.launch(headless=settings.playwright_headless)
        yield browser
        await browser.close()
    finally:
        await pw.stop()


# ---------------------------------------------------------------------------
# High-level page fetching
# ---------------------------------------------------------------------------
async def fetch_page_js(
    browser: Browser,
    url: str,
    *,
    wait_selector: str | None = None,
    wait_timeout_ms: int = 15_000,
    capture_inner_text: bool = False,
) -> str | tuple[str, str]:
    """Navigate to a URL in a new page, wait for content, and return the HTML.

    When *capture_inner_text* is ``True``, also evaluates
    ``document.body.innerText`` (which captures shadow-DOM content that
    ``page.content()`` misses) and returns ``(html, inner_text)``.
    """
    page: Page = await create_page(browser)
    try:
        log.info("Playwright GET %s", url)
        await page.goto(url, wait_until="commit", timeout=120_000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass  # best-effort — continue with whatever loaded

        if wait_selector:
            log.debug("Waiting for selector: %s", wait_selector)
            try:
                await page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
            except Exception:
                log.warning(
                    "wait_for_selector timed out after %dms for '%s' — "
                    "returning page HTML as-is",
                    wait_timeout_ms, wait_selector,
                )
        else:
            # Give JS-rendered content a moment to settle
            await asyncio.sleep(3)

        html = await page.content()

        if capture_inner_text:
            try:
                inner_text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''"
                )
            except Exception:
                inner_text = ""
            return html, inner_text

        return html
    finally:
        await page.close()


async def capture_screenshot(
    browser: Browser,
    url: str,
    *,
    wait_selector: str | None = None,
    full_page: bool = False,
) -> bytes:
    """Navigate to a URL and capture a PNG screenshot.

    Returns raw PNG bytes suitable for the vision model API.
    """
    page = await create_page(browser)
    try:
        await page.goto(url, wait_until="commit", timeout=120_000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:
                pass
        else:
            await asyncio.sleep(3)

        return await page.screenshot(full_page=full_page, type="png")
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

# Third-party domains whose JSON responses should never be considered listing APIs
_THIRD_PARTY_NOISE_DOMAINS = {
    "usercentrics.eu", "cookiebot.com", "onetrust.com", "trustarc.com",
    "google-analytics.com", "googletagmanager.com", "facebook.com",
    "facebook.net", "doubleclick.net", "googleapis.com", "gstatic.com",
    "cloudflare.com", "cdn-cgi", "sentry.io", "hotjar.com",
    "newrelic.com", "nr-data.net", "segment.io", "segment.com",
    "intercom.io", "zendesk.com", "crisp.chat", "tawk.to",
    "hubspot.com", "marketo.com", "pardot.com", "salesforce.com",
    "amplitude.com", "mixpanel.com", "heap.io", "fullstory.com",
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


def _pick_best_detail_api(captured: list[tuple[str, dict]]) -> tuple[str | None, dict | None]:
    """Pick the best-scoring detail API response from captured JSON bodies."""
    if not captured:
        return None, None

    scored = [(url, body, _score_api_response(url, body)) for url, body in captured]
    scored.sort(key=lambda item: item[2], reverse=True)

    for url, _, score in scored:
        log.debug("  API candidate: score=%d url=%s", score, url[:120])

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
    page = await create_page(browser)
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

    pending_tasks: set[asyncio.Task[None]] = set()
    listener: Callable[[Any], None] | None = None
    try:
        pending_tasks, listener = _register_async_response_listener(page, _on_response)
        await page.goto(url, wait_until="commit", timeout=120_000)

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:
                log.warning("wait_for_selector timed out for '%s' in intercept_detail_api — continuing", wait_selector)
        else:
            await asyncio.sleep(3)

        await _drain_pending_tasks(pending_tasks)

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
        await _drain_pending_tasks(pending_tasks)

        log.debug(
            "Captured %d JSON responses after click: %s",
            len(captured),
            [u[:100] for u, _ in captured],
        )

        if not captured:
            log.warning("No JSON API response captured after clicking detail button")
            return None, None

        return _pick_best_detail_api(captured)
    finally:
        if listener is not None:
            try:
                await _await_if_needed(page.remove_listener("response", listener))
            except Exception:
                pass
        await page.close()


async def intercept_detail_api_from_detail_url(
    browser: Browser,
    detail_url: str,
    *,
    wait_selector: str | None = None,
    timeout_ms: int = 15_000,
) -> tuple[str | None, dict | None]:
    """Navigate to a detail URL and capture the JSON API it triggers."""
    page = await create_page(browser)
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
                captured.append((response.url, {"_items": body}))
        except Exception as exc:
            log.debug("Failed to parse JSON from %s: %s", response.url[:100], exc)

    pending_tasks: set[asyncio.Task[None]] = set()
    listener: Callable[[Any], None] | None = None
    try:
        pending_tasks, listener = _register_async_response_listener(page, _on_response)
        await page.goto(detail_url, wait_until="commit", timeout=120_000)

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:
                log.warning(
                    "wait_for_selector timed out for '%s' in detail-url interception",
                    wait_selector,
                )
        else:
            await asyncio.sleep(3)

        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass
        await asyncio.sleep(2)
        await _drain_pending_tasks(pending_tasks)

        log.debug(
            "Captured %d JSON responses during detail navigation: %s",
            len(captured),
            [url[:100] for url, _ in captured],
        )
        if not captured:
            log.warning("No JSON API response captured while loading detail URL: %s", detail_url)
            return None, None

        return _pick_best_detail_api(captured)
    finally:
        if listener is not None:
            try:
                await _await_if_needed(page.remove_listener("response", listener))
            except Exception:
                pass
        await page.close()


# ---------------------------------------------------------------------------
# Listing API interception (captures XHR during page load)
# ---------------------------------------------------------------------------

# Common JSON keys that wrap a list of items
_LIST_CONTAINER_KEYS = ("data", "results", "items", "records", "entries",
                        "rows", "list", "objects", "content", "hits")

# URL keywords that suggest a listing/directory endpoint
_LISTING_URL_KEYWORDS = {
    "list", "search", "directory", "catalog", "exhibitor",
    "seller", "vendor", "company", "member", "partner",
}


def _find_items_list(body: Any) -> tuple[list[dict], str | None]:
    """Find the list of item dicts in a JSON response.

    Returns ``(items_list, json_path)`` or ``([], None)`` if not found.
    """
    if isinstance(body, list):
        items = [x for x in body if isinstance(x, dict)]
        return (items, None) if items else ([], None)

    if isinstance(body, dict):
        # Check known container keys (one level deep)
        for key in _LIST_CONTAINER_KEYS:
            val = body.get(key)
            if isinstance(val, list) and val and isinstance(val[0], dict):
                return val, key

        # Check any key that holds a list of dicts
        for key, val in body.items():
            if isinstance(val, list) and len(val) >= 3 and isinstance(val[0], dict):
                return val, key

        # Check one level of nesting (e.g. body.data.results)
        for outer_key, outer_val in body.items():
            if isinstance(outer_val, dict):
                for inner_key in _LIST_CONTAINER_KEYS:
                    val = outer_val.get(inner_key)
                    if isinstance(val, list) and val and isinstance(val[0], dict):
                        return val, f"{outer_key}.{inner_key}"

    return [], None


def _score_listing_api_response(
    api_url: str, body: Any, *, page_domain: str = "",
) -> tuple[int, list[dict], str | None]:
    """Score a JSON response on how likely it is to be a listing API.

    Returns ``(score, items_list, json_path)``.  Negative score = noise.
    *page_domain* is the domain of the page being scraped — responses from
    different domains are heavily penalized.
    """
    url_lower = api_url.lower()

    # Penalize noise URLs heavily
    for frag in _NOISE_URL_FRAGMENTS:
        if frag in url_lower:
            return -1000, [], None

    # Penalize known third-party tracking/consent domains
    try:
        from urllib.parse import urlparse as _urlparse
        api_domain = _urlparse(api_url).hostname or ""
    except Exception:
        api_domain = ""
    for noise_domain in _THIRD_PARTY_NOISE_DOMAINS:
        if api_domain.endswith(noise_domain):
            return -1000, [], None

    # Penalize cross-domain responses (third-party widgets/SDKs)
    if page_domain and api_domain:
        page_root = ".".join(page_domain.rsplit(".", 2)[-2:])
        api_root = ".".join(api_domain.rsplit(".", 2)[-2:])
        if page_root != api_root:
            # Cross-domain — heavy penalty (but not outright rejection
            # in case there's a legitimate separate API domain)
            return -500, [], None

    items, json_path = _find_items_list(body)
    if not items:
        return -500, [], None  # no list found — probably not a listing API

    # Score based on item content
    sample = items[:5]
    detail_hits = 0
    for item in sample:
        item_keys = {k.lower() for k in item.keys()}
        if isinstance(item, dict):
            for v in item.values():
                if isinstance(v, dict):
                    item_keys.update(kk.lower() for kk in v.keys())
        detail_hits += sum(1 for k in item_keys if any(dk in k for dk in _DETAIL_DATA_KEYS))

    # List length bonus
    length_bonus = 0
    if len(items) > 20:
        length_bonus = 100
    elif len(items) > 5:
        length_bonus = 50

    # URL keyword bonus
    url_bonus = sum(15 for kw in _LISTING_URL_KEYWORDS if kw in url_lower)

    score = detail_hits * 10 + length_bonus + url_bonus + len(items)
    return score, items, json_path


async def intercept_listing_api(
    browser: Browser,
    url: str,
    *,
    wait_selector: str | None = None,
    expected_fields: list[str] | None = None,
    timeout_ms: int = 20_000,
) -> tuple[str | None, list[dict] | None, str | None]:
    """Capture JSON listing API calls during page load.

    Listens for all JSON network responses during navigation and scoring
    each one to find the most likely listing API (one that returns an array
    of items with seller/company-like keys).

    Returns ``(api_url, items_list, json_path)`` or ``(None, None, None)``.
    """
    page = await create_page(browser)
    captured: list[tuple[str, Any]] = []

    async def _on_response(response):  # type: ignore[no-untyped-def]
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            return
        try:
            body = await response.json()
            captured.append((response.url, body))
        except Exception:
            pass

    pending_tasks: set[asyncio.Task[None]] = set()
    listener: Callable[[Any], None] | None = None
    try:
        pending_tasks, listener = _register_async_response_listener(page, _on_response)
        await page.goto(url, wait_until="commit", timeout=120_000)

        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=15_000)
            except Exception:
                pass
        else:
            await asyncio.sleep(3)

        # Wait for network to settle — listing APIs may load after initial render
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass
        await asyncio.sleep(2)  # extra buffer for late API calls
        await _drain_pending_tasks(pending_tasks)

        log.debug(
            "Listing API interception: captured %d JSON responses from %s",
            len(captured), url,
        )

        if not captured:
            return None, None, None

        # Extract the page domain for same-origin filtering
        from urllib.parse import urlparse as _urlparse
        _page_domain = _urlparse(url).hostname or ""

        # Score all captured responses
        scored = [
            (u, items, path, score)
            for u, body in captured
            for score, items, path in [_score_listing_api_response(u, body, page_domain=_page_domain)]
        ]
        scored.sort(key=lambda x: x[3], reverse=True)

        for u, _, _, s in scored[:5]:
            log.debug("  Listing API candidate: score=%d url=%s", s, u[:120])

        best_url, best_items, best_path, best_score = scored[0]

        if best_score < 0:
            log.info(
                "No listing API found among %d responses (best score=%d)",
                len(scored), best_score,
            )
            return None, None, None

        log.info(
            "Captured listing API: %s (score=%d, %d items, path=%s)",
            best_url[:120], best_score, len(best_items), best_path,
        )
        return best_url, best_items, best_path
    finally:
        if listener is not None:
            try:
                await _await_if_needed(page.remove_listener("response", listener))
            except Exception:
                pass
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
    page = await create_page(browser)
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

    for pg_url in urls:
        if max_items is not None and len(htmls) * items_per_page >= max_items:
            break
        try:
            # Check if the page is already at this URL (avoid redundant navigation)
            if page.url != pg_url:
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


# ------------------------------------------------------------------
# Screenshot capture
# ------------------------------------------------------------------
async def take_screenshot(
    browser: Browser,
    url: str,
    output_path: str | Path,
    *,
    wait_selector: str | None = None,
    full_page: bool = False,
) -> str:
    """Navigate to *url* and save a screenshot to *output_path*.

    Returns the absolute file path of the saved screenshot.
    """
    from pathlib import Path as P

    page: Page = await create_page(browser)
    try:
        await page.goto(url, wait_until="commit", timeout=60_000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:
            pass
        if wait_selector:
            try:
                await page.wait_for_selector(wait_selector, timeout=10_000)
            except Exception:
                pass
        else:
            await asyncio.sleep(2)

        out = P(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(out), full_page=full_page)
        log.info("Screenshot saved: %s", out)
        return str(out.resolve())
    finally:
        await page.close()


# ------------------------------------------------------------------
# Console log monitoring
# ------------------------------------------------------------------
@dataclass
class ConsoleEntry:
    """A captured browser console message."""

    level: str  # "log", "warning", "error", "info"
    text: str
    url: str = ""


async def fetch_page_js_with_console(
    browser: Browser,
    url: str,
    *,
    wait_selector: str | None = None,
    wait_timeout_ms: int = 15_000,
) -> tuple[str, list[ConsoleEntry]]:
    """Like :func:`fetch_page_js` but also captures console messages.

    Returns ``(html, console_entries)``.
    """
    entries: list[ConsoleEntry] = []
    page: Page = await create_page(browser)
    try:
        def _on_console(msg):
            entries.append(ConsoleEntry(
                level=msg.type,
                text=msg.text,
                url=url,
            ))

        page.on("console", _on_console)

        await page.goto(url, wait_until="commit", timeout=120_000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:
            pass
        if wait_selector:
            await page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
        else:
            await asyncio.sleep(3)

        html = await page.content()
        log.debug("Captured %d console entries from %s", len(entries), url)
        return html, entries
    finally:
        await page.close()
