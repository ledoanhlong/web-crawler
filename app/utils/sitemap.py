"""Sitemap & robots.txt utilities.

Discovers listing URLs from ``/sitemap.xml`` (and nested sitemaps) and
checks ``/robots.txt`` for crawl rules.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

from app.config import settings
from app.utils.logging import get_logger

log = get_logger(__name__)

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _base_url(url: str) -> str:
    """Return scheme + netloc from a URL."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def parse_robots_txt(text: str, *, user_agent: str = "*") -> dict:
    """Parse robots.txt into a simple dict of rules.

    Returns
    -------
    dict
        ``{"disallow": ["/admin/", ...], "sitemaps": ["https://…/sitemap.xml"], "crawl_delay": 2}``
    """
    disallow: list[str] = []
    sitemaps: list[str] = []
    crawl_delay: float | None = None

    current_ua_matches = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.lower().startswith("user-agent:"):
            ua = line.split(":", 1)[1].strip()
            current_ua_matches = ua == "*" or ua.lower() == user_agent.lower()
        elif current_ua_matches and line.lower().startswith("disallow:"):
            path = line.split(":", 1)[1].strip()
            if path:
                disallow.append(path)
        elif line.lower().startswith("sitemap:"):
            sm = line.split(":", 1)[1].strip()
            if sm:
                # Reconstruct full URL if the line was split on the http: colon
                if sm.startswith("//"):
                    sm = "https:" + sm
                elif not sm.startswith("http"):
                    # handle 'Sitemap: https://...' where split ate the scheme colon
                    sm = "https:" + sm if sm.startswith("//") else line.split("Sitemap:", 1)[-1].split("sitemap:", 1)[-1].strip()
                sitemaps.append(sm)
        elif current_ua_matches and line.lower().startswith("crawl-delay:"):
            try:
                crawl_delay = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass

    return {"disallow": disallow, "sitemaps": sitemaps, "crawl_delay": crawl_delay}


def is_url_allowed(url: str, disallow_rules: list[str]) -> bool:
    """Check whether *url*'s path is allowed given robots.txt Disallow rules."""
    path = urlparse(url).path
    for rule in disallow_rules:
        if rule.endswith("*"):
            if path.startswith(rule[:-1]):
                return False
        elif path.startswith(rule):
            return False
    return True


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------

def parse_sitemap_xml(xml_text: str) -> list[str]:
    """Extract all ``<loc>`` URLs from a sitemap XML document.

    Handles both sitemap index files and regular sitemaps.
    Returns a flat list of URLs (sitemap locations or page URLs).
    """
    urls: list[str] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.debug("Sitemap XML parse error: %s", exc)
        return urls

    # Strip namespace for easier matching
    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag

    # <sitemapindex> — contains nested <sitemap><loc> entries
    for loc in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc"):
        if loc.text:
            urls.append(loc.text.strip())

    # Fallback: try without namespace
    if not urls:
        for loc in root.iter("loc"):
            if loc.text:
                urls.append(loc.text.strip())

    return urls


async def discover_sitemap_urls(
    base_url: str,
    *,
    fetch_fn=None,
    max_sitemaps: int = 5,
    max_urls: int = 10_000,
) -> list[str]:
    """Discover page URLs from sitemaps starting at *base_url*.

    1. Fetches ``/robots.txt`` to find declared sitemaps.
    2. Falls back to ``/sitemap.xml`` if none declared.
    3. Recursively follows sitemap index files.

    Parameters
    ----------
    fetch_fn : callable, optional
        Async function ``url -> str`` for fetching pages.  Defaults to
        :func:`app.utils.http.fetch_page`.
    """
    if not settings.use_sitemap_discovery:
        log.debug("Sitemap discovery disabled")
        return []

    if fetch_fn is None:
        from app.utils.http import fetch_page
        fetch_fn = fetch_page

    root = _base_url(base_url)
    all_urls: list[str] = []
    sitemap_queue: list[str] = []
    visited: set[str] = set()

    # 1. Try robots.txt
    try:
        robots_text = await fetch_fn(f"{root}/robots.txt")
        rules = parse_robots_txt(robots_text)
        sitemap_queue.extend(rules["sitemaps"])
        log.info("robots.txt listed %d sitemap(s)", len(rules["sitemaps"]))
    except Exception:
        log.debug("robots.txt not available at %s", root)

    # 2. Fallback
    if not sitemap_queue:
        sitemap_queue.append(f"{root}/sitemap.xml")

    # 3. Process sitemaps
    processed = 0
    while sitemap_queue and processed < max_sitemaps and len(all_urls) < max_urls:
        sm_url = sitemap_queue.pop(0)
        if sm_url in visited:
            continue
        visited.add(sm_url)
        try:
            xml_text = await fetch_fn(sm_url)
        except Exception:
            log.debug("Could not fetch sitemap %s", sm_url)
            continue
        processed += 1

        urls = parse_sitemap_xml(xml_text)
        for u in urls:
            if u.endswith(".xml") or u.endswith(".xml.gz"):
                if u not in visited:
                    sitemap_queue.append(u)
            else:
                all_urls.append(u)
            if len(all_urls) >= max_urls:
                break

    log.info("Sitemap discovery found %d URL(s) from %d sitemap(s)", len(all_urls), processed)
    return all_urls
