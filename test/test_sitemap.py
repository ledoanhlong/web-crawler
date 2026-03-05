"""Tests for app.utils.sitemap — robots.txt and sitemap parsing."""

from __future__ import annotations

import pytest

from app.utils.sitemap import (
    parse_robots_txt,
    is_url_allowed,
    parse_sitemap_xml,
    discover_sitemap_urls,
)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

ROBOTS_TXT = """\
User-agent: *
Disallow: /admin/
Disallow: /private/
Crawl-delay: 2

User-agent: Googlebot
Disallow: /no-google/

Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/sitemap-products.xml
"""


class TestParseRobotsTxt:
    def test_disallow_rules(self):
        rules = parse_robots_txt(ROBOTS_TXT)
        assert "/admin/" in rules["disallow"]
        assert "/private/" in rules["disallow"]

    def test_sitemaps(self):
        rules = parse_robots_txt(ROBOTS_TXT)
        assert "https://example.com/sitemap.xml" in rules["sitemaps"]
        assert "https://example.com/sitemap-products.xml" in rules["sitemaps"]

    def test_crawl_delay(self):
        rules = parse_robots_txt(ROBOTS_TXT)
        assert rules["crawl_delay"] == 2.0

    def test_empty(self):
        rules = parse_robots_txt("")
        assert rules["disallow"] == []
        assert rules["sitemaps"] == []
        assert rules["crawl_delay"] is None


class TestIsUrlAllowed:
    def test_allowed(self):
        assert is_url_allowed("https://example.com/page", ["/admin/"])

    def test_disallowed(self):
        assert not is_url_allowed("https://example.com/admin/settings", ["/admin/"])

    def test_wildcard_rule(self):
        assert not is_url_allowed("https://example.com/tmp/file.txt", ["/tmp*"])

    def test_empty_rules(self):
        assert is_url_allowed("https://example.com/anything", [])


# ---------------------------------------------------------------------------
# Sitemap XML
# ---------------------------------------------------------------------------

SITEMAP_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page-1</loc></url>
  <url><loc>https://example.com/page-2</loc></url>
  <url><loc>https://example.com/page-3</loc></url>
</urlset>
"""

SITEMAP_INDEX_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap>
</sitemapindex>
"""


class TestParseSitemapXml:
    def test_urlset(self):
        urls = parse_sitemap_xml(SITEMAP_XML)
        assert len(urls) == 3
        assert "https://example.com/page-1" in urls

    def test_sitemap_index(self):
        urls = parse_sitemap_xml(SITEMAP_INDEX_XML)
        assert len(urls) == 2
        assert "https://example.com/sitemap-1.xml" in urls

    def test_malformed(self):
        urls = parse_sitemap_xml("<not valid xml")
        assert urls == []

    def test_empty(self):
        urls = parse_sitemap_xml('<?xml version="1.0"?><urlset></urlset>')
        assert urls == []


# ---------------------------------------------------------------------------
# Discover sitemap URLs
# ---------------------------------------------------------------------------

class TestDiscoverSitemapUrls:
    @pytest.mark.asyncio
    async def test_discovery_with_robots(self):
        """Mock fetch to simulate robots.txt → sitemap → URLs."""
        call_log: list[str] = []

        async def mock_fetch(url: str) -> str:
            call_log.append(url)
            if url.endswith("/robots.txt"):
                return "Sitemap: https://example.com/sitemap.xml"
            if url.endswith("/sitemap.xml"):
                return SITEMAP_XML
            return ""

        urls = await discover_sitemap_urls("https://example.com/dir/page", fetch_fn=mock_fetch)
        assert len(urls) == 3
        assert "https://example.com/page-1" in urls
        assert any("/robots.txt" in c for c in call_log)

    @pytest.mark.asyncio
    async def test_fallback_to_default_sitemap(self):
        """When robots.txt fails, should try /sitemap.xml."""
        async def mock_fetch(url: str) -> str:
            if url.endswith("/robots.txt"):
                raise Exception("404")
            if url.endswith("/sitemap.xml"):
                return SITEMAP_XML
            return ""

        urls = await discover_sitemap_urls("https://example.com", fetch_fn=mock_fetch)
        assert len(urls) == 3

    @pytest.mark.asyncio
    async def test_sitemap_index_followed(self):
        """Sitemap index files should be followed recursively."""
        async def mock_fetch(url: str) -> str:
            if url.endswith("/robots.txt"):
                return "Sitemap: https://example.com/sitemap-index.xml"
            if url.endswith("/sitemap-index.xml"):
                return SITEMAP_INDEX_XML
            if url.endswith("/sitemap-1.xml"):
                return SITEMAP_XML
            if url.endswith("/sitemap-2.xml"):
                return '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.com/page-4</loc></url></urlset>'
            return ""

        urls = await discover_sitemap_urls("https://example.com", fetch_fn=mock_fetch)
        assert "https://example.com/page-1" in urls
        assert "https://example.com/page-4" in urls
