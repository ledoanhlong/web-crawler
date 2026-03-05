"""Tests for app.utils.structured_data — JSON-LD, Open Graph, Microdata extraction."""

from __future__ import annotations

import pytest

from app.utils.structured_data import (
    extract_json_ld,
    extract_open_graph,
    extract_microdata,
    extract_all_structured_data,
)


# ---------------------------------------------------------------------------
# JSON-LD extraction
# ---------------------------------------------------------------------------

HTML_WITH_JSON_LD = """
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Organization",
  "name": "Acme Corp",
  "url": "https://acme.example.com",
  "telephone": "+1-555-0100",
  "address": {
    "@type": "PostalAddress",
    "streetAddress": "123 Main St",
    "addressLocality": "Springfield",
    "addressCountry": "US"
  }
}
</script>
</head>
<body><p>Content</p></body>
</html>
"""

HTML_WITH_GRAPH = """
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": "Organization", "name": "Corp A"},
    {"@type": "WebPage", "name": "Directory"},
    {"@type": "BreadcrumbList", "name": "Nav"}
  ]
}
</script>
</head>
<body></body>
</html>
"""

HTML_WITH_PRODUCT_LIST = """
<html><head>
<script type="application/ld+json">
[
  {"@type": "Product", "name": "Widget A", "sku": "W001"},
  {"@type": "Product", "name": "Widget B", "sku": "W002"}
]
</script>
</head><body></body></html>
"""

HTML_WITH_MALFORMED_LD = """
<html><head>
<script type="application/ld+json">
{this is not valid json}
</script>
<script type="application/ld+json">
{"@type": "LocalBusiness", "name": "Good Data"}
</script>
</head><body></body></html>
"""


class TestExtractJsonLd:
    def test_single_organization(self):
        items = extract_json_ld(HTML_WITH_JSON_LD)
        assert len(items) == 1
        assert items[0]["@type"] == "Organization"
        assert items[0]["name"] == "Acme Corp"

    def test_graph_filters_relevant_types(self):
        items = extract_json_ld(HTML_WITH_GRAPH)
        types = [i["@type"] for i in items]
        assert "Organization" in types
        assert "WebPage" in types
        # BreadcrumbList is not in _RELEVANT_LD_TYPES
        assert "BreadcrumbList" not in types

    def test_list_of_products(self):
        items = extract_json_ld(HTML_WITH_PRODUCT_LIST)
        assert len(items) == 2
        assert items[0]["name"] == "Widget A"

    def test_malformed_skipped(self):
        items = extract_json_ld(HTML_WITH_MALFORMED_LD)
        assert len(items) == 1
        assert items[0]["name"] == "Good Data"

    def test_no_json_ld(self):
        items = extract_json_ld("<html><body>No LD</body></html>")
        assert items == []

    def test_nested_address(self):
        items = extract_json_ld(HTML_WITH_JSON_LD)
        addr = items[0].get("address", {})
        assert addr.get("addressLocality") == "Springfield"


# ---------------------------------------------------------------------------
# Open Graph extraction
# ---------------------------------------------------------------------------

HTML_WITH_OG = """
<html>
<head>
  <meta property="og:title" content="Acme Corp Directory">
  <meta property="og:description" content="Browse our exhibitors">
  <meta property="og:image" content="https://example.com/og-image.jpg">
  <meta property="og:url" content="https://example.com/directory">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="Acme Corp">
</head>
<body></body>
</html>
"""


class TestExtractOpenGraph:
    def test_og_tags(self):
        og = extract_open_graph(HTML_WITH_OG)
        assert og["og:title"] == "Acme Corp Directory"
        assert og["og:description"] == "Browse our exhibitors"
        assert og["og:image"] == "https://example.com/og-image.jpg"

    def test_twitter_tags(self):
        og = extract_open_graph(HTML_WITH_OG)
        assert og["twitter:card"] == "summary_large_image"
        assert og["twitter:title"] == "Acme Corp"

    def test_no_og_tags(self):
        og = extract_open_graph("<html><body>No OG</body></html>")
        assert og == {}

    def test_empty_content_skipped(self):
        html = '<html><head><meta property="og:title" content=""></head><body></body></html>'
        og = extract_open_graph(html)
        assert "og:title" not in og


# ---------------------------------------------------------------------------
# Microdata extraction
# ---------------------------------------------------------------------------

HTML_WITH_MICRODATA = """
<html><body>
<div itemscope itemtype="https://schema.org/Organization">
  <span itemprop="name">TechCo</span>
  <a itemprop="url" href="https://techco.example.com">Website</a>
  <span itemprop="telephone">+49-30-12345</span>
  <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
    <span itemprop="streetAddress">Berliner Str. 1</span>
    <span itemprop="addressLocality">Berlin</span>
    <meta itemprop="addressCountry" content="DE">
  </div>
</div>
</body></html>
"""

HTML_WITH_MULTIPLE_SCOPES = """
<html><body>
<div itemscope itemtype="https://schema.org/Organization">
  <span itemprop="name">Corp A</span>
</div>
<div itemscope itemtype="https://schema.org/Organization">
  <span itemprop="name">Corp B</span>
</div>
</body></html>
"""


class TestExtractMicrodata:
    def test_basic_organization(self):
        items = extract_microdata(HTML_WITH_MICRODATA)
        assert len(items) == 1
        assert items[0]["type"] == "Organization"
        props = items[0]["properties"]
        assert props["name"] == "TechCo"
        assert props["url"] == "https://techco.example.com"
        assert props["telephone"] == "+49-30-12345"

    def test_nested_address(self):
        items = extract_microdata(HTML_WITH_MICRODATA)
        addr = items[0]["properties"]["address"]
        assert isinstance(addr, dict)
        assert addr["type"] == "PostalAddress"
        assert addr["properties"]["addressLocality"] == "Berlin"
        assert addr["properties"]["addressCountry"] == "DE"

    def test_multiple_scopes(self):
        items = extract_microdata(HTML_WITH_MULTIPLE_SCOPES)
        assert len(items) == 2
        names = [i["properties"]["name"] for i in items]
        assert "Corp A" in names
        assert "Corp B" in names

    def test_no_microdata(self):
        items = extract_microdata("<html><body>No microdata</body></html>")
        assert items == []


# ---------------------------------------------------------------------------
# Unified extraction
# ---------------------------------------------------------------------------

class TestExtractAllStructuredData:
    def test_returns_all_keys(self):
        result = extract_all_structured_data("<html><body></body></html>")
        assert "json_ld" in result
        assert "open_graph" in result
        assert "microdata" in result

    def test_empty_page(self):
        result = extract_all_structured_data("<html><body></body></html>")
        assert result["json_ld"] == []
        assert result["open_graph"] == {}
        assert result["microdata"] == []

    def test_combined(self):
        html = HTML_WITH_JSON_LD.replace("</head>", """
          <meta property="og:title" content="Test">
        </head>""")
        result = extract_all_structured_data(html)
        assert len(result["json_ld"]) == 1
        assert result["open_graph"]["og:title"] == "Test"
