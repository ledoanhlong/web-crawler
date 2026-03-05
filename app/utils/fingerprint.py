"""Platform fingerprinting — detect CMS / e-commerce / SPA framework.

Examines HTML, HTTP headers, and meta tags to identify the technology
stack powering a website (e.g. Shopify, WordPress, Next.js, Angular).
The detected platform helps the planner and router agents choose better
extraction strategies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class PlatformInfo:
    """Detected platform / technology stack of a website."""

    cms: str | None = None  # WordPress, Drupal, Joomla, etc.
    ecommerce: str | None = None  # Shopify, Magento, WooCommerce, etc.
    js_framework: str | None = None  # React, Angular, Vue, Next.js, Nuxt, etc.
    cdn: str | None = None  # Cloudflare, Akamai, Fastly, etc.
    server: str | None = None  # nginx, Apache, IIS, etc.
    has_api: bool = False  # Evidence of REST/GraphQL API
    is_spa: bool = False  # Single-page application detected
    signals: list[str] = field(default_factory=list)  # Human-readable signals

    def summary(self) -> str:
        """Return a compact one-line summary."""
        parts = []
        if self.cms:
            parts.append(f"CMS={self.cms}")
        if self.ecommerce:
            parts.append(f"Ecommerce={self.ecommerce}")
        if self.js_framework:
            parts.append(f"JS={self.js_framework}")
        if self.is_spa:
            parts.append("SPA")
        if self.has_api:
            parts.append("API")
        if self.cdn:
            parts.append(f"CDN={self.cdn}")
        if self.server:
            parts.append(f"Server={self.server}")
        return " | ".join(parts) if parts else "Unknown"

    def to_dict(self) -> dict:
        return {
            "cms": self.cms,
            "ecommerce": self.ecommerce,
            "js_framework": self.js_framework,
            "cdn": self.cdn,
            "server": self.server,
            "has_api": self.has_api,
            "is_spa": self.is_spa,
            "signals": self.signals,
        }


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

_CMS_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("WordPress", re.compile(r'wp-content|wp-includes|wp-json|wordpress', re.I)),
    ("Drupal", re.compile(r'sites/default/files|drupal\.js|Drupal\.settings', re.I)),
    ("Joomla", re.compile(r'/media/jui/|/components/com_', re.I)),
    ("Typo3", re.compile(r'typo3conf/|typo3temp/', re.I)),
    ("Contentful", re.compile(r'contentful\.com|ctfassets\.net', re.I)),
    ("Prismic", re.compile(r'prismic\.io', re.I)),
    ("Strapi", re.compile(r'strapi', re.I)),
    ("HubSpot", re.compile(r'hs-scripts\.com|hubspot\.net|hbspt', re.I)),
    ("Wix", re.compile(r'wix\.com|wixstatic\.com|_wix_browser_sess', re.I)),
    ("Squarespace", re.compile(r'squarespace\.com|sqsp\.net|static\.squarespace', re.I)),
    ("Webflow", re.compile(r'webflow\.com|assets\.website-files\.com', re.I)),
]

_ECOMMERCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Shopify", re.compile(r'cdn\.shopify\.com|shopify\.com|Shopify\.theme', re.I)),
    ("Magento", re.compile(r'mage/|Magento_|magento|mage-init', re.I)),
    ("WooCommerce", re.compile(r'woocommerce|wc-ajax|wc_add_to_cart', re.I)),
    ("BigCommerce", re.compile(r'bigcommerce\.com|cdn11\.bigcommerce', re.I)),
    ("PrestaShop", re.compile(r'prestashop|modules/ps_', re.I)),
    ("Salesforce Commerce", re.compile(r'demandware\.net|sfcc|salesforce.*commerce', re.I)),
    ("SAP Commerce", re.compile(r'hybris|sap-commerce', re.I)),
]

_JS_FRAMEWORK_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Next.js", re.compile(r'__next|_next/static|__NEXT_DATA__', re.I)),
    ("Nuxt", re.compile(r'__nuxt|_nuxt/|__NUXT__', re.I)),
    ("Gatsby", re.compile(r'gatsby-|___gatsby|gatsby-chunk-mapping', re.I)),
    ("Angular", re.compile(r'ng-version|angular\.json|ng-app', re.I)),
    ("React", re.compile(r'__react|react-root|_reactRootContainer|data-reactroot|reactjs', re.I)),
    ("Vue.js", re.compile(r'__vue|vue\.runtime|v-cloak|vue-app', re.I)),
    ("Svelte", re.compile(r'__svelte|svelte-', re.I)),
    ("Ember", re.compile(r'ember\.js|ember-cli|__ember', re.I)),
]

_SPA_MARKERS: list[re.Pattern] = [
    re.compile(r'<div\s+id="(?:app|root|__next|__nuxt|___gatsby)"', re.I),
    re.compile(r'ng-app|ng-version', re.I),
    re.compile(r'data-reactroot|_reactRootContainer', re.I),
    re.compile(r'__NEXT_DATA__|__NUXT__', re.I),
]


def _detect_from_headers(headers: dict[str, str], info: PlatformInfo) -> None:
    """Detect CDN / server from HTTP response headers."""
    server = headers.get("server", "")
    if server:
        info.server = server.split("/")[0]
        info.signals.append(f"Server: {server}")

    # CDN detection
    if "cf-ray" in headers or "cf-cache-status" in headers:
        info.cdn = "Cloudflare"
        info.signals.append("CDN: Cloudflare (cf-ray)")
    elif "x-amz-cf-id" in headers:
        info.cdn = "CloudFront"
        info.signals.append("CDN: CloudFront")
    elif "x-fastly-request-id" in headers:
        info.cdn = "Fastly"
        info.signals.append("CDN: Fastly")
    elif "x-akamai-transformed" in headers or "akamai" in headers.get("via", "").lower():
        info.cdn = "Akamai"
        info.signals.append("CDN: Akamai")

    # Powered-by
    powered = headers.get("x-powered-by", "")
    if powered:
        info.signals.append(f"X-Powered-By: {powered}")
        if "next.js" in powered.lower():
            info.js_framework = info.js_framework or "Next.js"
        elif "express" in powered.lower():
            info.signals.append("Node/Express backend detected")

    # API evidence
    ct = headers.get("content-type", "")
    if "application/json" in ct or "graphql" in ct:
        info.has_api = True
        info.signals.append(f"API evidence: Content-Type={ct}")


def _detect_from_html(html: str, info: PlatformInfo) -> None:
    """Detect CMS/ecommerce/JS framework from HTML source."""
    # Check a limited slice (first 50k chars) for efficiency
    snippet = html[:50_000]

    for name, pattern in _CMS_PATTERNS:
        if pattern.search(snippet):
            info.cms = info.cms or name
            info.signals.append(f"CMS signal: {name}")
            break

    for name, pattern in _ECOMMERCE_PATTERNS:
        if pattern.search(snippet):
            info.ecommerce = info.ecommerce or name
            info.signals.append(f"Ecommerce signal: {name}")
            break

    for name, pattern in _JS_FRAMEWORK_PATTERNS:
        if pattern.search(snippet):
            info.js_framework = info.js_framework or name
            info.signals.append(f"JS framework signal: {name}")
            break

    # SPA detection
    for marker in _SPA_MARKERS:
        if marker.search(snippet):
            info.is_spa = True
            info.signals.append(f"SPA marker: {marker.pattern[:40]}")
            break

    # API evidence from HTML
    api_patterns = [
        re.compile(r'/api/v\d|/api/graphql|/graphql', re.I),
        re.compile(r'__NEXT_DATA__|window\.__INITIAL_STATE__', re.I),
        re.compile(r'application/ld\+json', re.I),
    ]
    for pat in api_patterns:
        if pat.search(snippet):
            info.has_api = True
            info.signals.append(f"API evidence in HTML: {pat.pattern[:40]}")
            break

    # Meta generator tag
    gen_match = re.search(
        r'<meta\s+name=["\']generator["\']\s+content=["\']([^"\']+)["\']',
        snippet, re.I,
    )
    if gen_match:
        gen = gen_match.group(1)
        info.signals.append(f"Generator: {gen}")
        gen_lower = gen.lower()
        if "wordpress" in gen_lower:
            info.cms = info.cms or "WordPress"
        elif "drupal" in gen_lower:
            info.cms = info.cms or "Drupal"
        elif "joomla" in gen_lower:
            info.cms = info.cms or "Joomla"


def fingerprint(
    html: str,
    headers: dict[str, str] | None = None,
) -> PlatformInfo:
    """Detect the platform / technology stack from HTML and optional headers.

    Parameters
    ----------
    html : str
        Raw HTML of the page.
    headers : dict, optional
        Lowercased HTTP response headers.

    Returns
    -------
    PlatformInfo
        Detected platform information.
    """
    info = PlatformInfo()
    if headers:
        _detect_from_headers(headers, info)
    _detect_from_html(html, info)
    log.info("Platform fingerprint: %s", info.summary())
    return info
