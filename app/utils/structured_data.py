"""Structured data extraction — JSON-LD, Open Graph, Microdata.

Extracts machine-readable structured data from raw HTML *before*
``simplify_html`` strips ``<script>`` tags.  This data is authoritative
and can supplement or replace CSS-selector-based extraction.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from app.utils.logging import get_logger

log = get_logger(__name__)

# Schema.org types we consider relevant for company/seller data
_RELEVANT_LD_TYPES: set[str] = {
    "Organization",
    "LocalBusiness",
    "Corporation",
    "Person",
    "Store",
    "Brand",
    "Product",
    "Event",
    "ItemList",
    "ListItem",
    "WebPage",
    "ProfilePage",
}


def extract_json_ld(html: str) -> list[dict]:
    """Parse all ``<script type="application/ld+json">`` blocks.

    Returns a list of parsed JSON objects, filtered to include only
    those whose ``@type`` is in :data:`_RELEVANT_LD_TYPES`.
    If a block contains a ``@graph`` array, each item is checked individually.
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.debug("Malformed JSON-LD block skipped")
            continue

        # Handle @graph arrays
        items = data.get("@graph", [data]) if isinstance(data, dict) else ([data] if isinstance(data, dict) else data)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            ld_type = item.get("@type", "")
            # @type can be a string or list
            types = [ld_type] if isinstance(ld_type, str) else (ld_type if isinstance(ld_type, list) else [])
            if any(t in _RELEVANT_LD_TYPES for t in types) or not types:
                results.append(item)

    log.debug("Extracted %d JSON-LD items from HTML", len(results))
    return results


def extract_open_graph(html: str) -> dict[str, str]:
    """Parse ``<meta property="og:*">`` tags into a flat dict.

    Example output: ``{"og:title": "Acme Corp", "og:description": "..."}``
    """
    soup = BeautifulSoup(html, "lxml")
    og: dict[str, str] = {}
    for meta in soup.find_all("meta", attrs={"property": re.compile(r"^og:")}):
        prop = meta.get("property", "")
        content = meta.get("content", "")
        if prop and content:
            og[prop] = content

    # Also grab Twitter Card tags
    for meta in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:")}):
        name = meta.get("name", "")
        content = meta.get("content", "")
        if name and content:
            og[name] = content

    log.debug("Extracted %d Open Graph / Twitter Card tags", len(og))
    return og


def extract_microdata(html: str) -> list[dict]:
    """Parse HTML Microdata (``itemscope`` / ``itemprop``) into nested dicts.

    Returns a list of top-level scoped items.  Each is a dict with
    ``type`` (from ``itemtype``) and ``properties`` (from ``itemprop``
    values).
    """
    soup = BeautifulSoup(html, "lxml")
    results: list[dict] = []

    for scope in soup.find_all(attrs={"itemscope": True}):
        # Only top-level scopes (not nested inside another scope)
        if scope.find_parent(attrs={"itemscope": True}):
            continue
        item = _parse_microdata_scope(scope)
        if item:
            results.append(item)

    log.debug("Extracted %d Microdata items", len(results))
    return results


def _parse_microdata_scope(element) -> dict:
    """Recursively parse a single itemscope element."""
    item: dict = {}
    itemtype = element.get("itemtype", "")
    if itemtype:
        # Extract type name from URL like "https://schema.org/Organization"
        item["type"] = itemtype.rsplit("/", 1)[-1] if "/" in itemtype else itemtype

    properties: dict[str, str | dict | list] = {}
    for child in element.find_all(attrs={"itemprop": True}):
        # Skip if this itemprop belongs to a nested scope
        parent_scope = child.find_parent(attrs={"itemscope": True})
        if parent_scope and parent_scope != element:
            continue

        prop_name = child.get("itemprop", "")
        if not prop_name:
            continue

        if child.has_attr("itemscope"):
            value = _parse_microdata_scope(child)
        elif child.name == "meta":
            value = child.get("content", "")
        elif child.name in ("a", "link"):
            value = child.get("href", child.get_text(strip=True))
        elif child.name == "img":
            value = child.get("src", child.get("alt", ""))
        elif child.name == "time":
            value = child.get("datetime", child.get_text(strip=True))
        else:
            value = child.get_text(strip=True)

        # If property already exists, make it a list
        if prop_name in properties:
            existing = properties[prop_name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                properties[prop_name] = [existing, value]
        else:
            properties[prop_name] = value

    item["properties"] = properties
    return item


def extract_all_structured_data(html: str) -> dict:
    """Unified entry point — extract all structured data from raw HTML.

    Returns::

        {
            "json_ld": [<list of JSON-LD objects>],
            "open_graph": {<og:* and twitter:* tags>},
            "microdata": [<list of microdata items>],
        }

    The result is empty (all keys are empty collections) if no structured
    data is found.
    """
    return {
        "json_ld": extract_json_ld(html),
        "open_graph": extract_open_graph(html),
        "microdata": extract_microdata(html),
    }
