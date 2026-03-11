"""Tests for ScraperAgent._extract_items() and related extraction logic.

These tests validate the core CSS selector extraction without needing
network access or an LLM — they use pre-built HTML and ScrapingTargets.
"""

from __future__ import annotations

import pytest

from app.agents.scraper_agent import ScraperAgent
from app.models.schemas import DetailApiPlan, ScrapingTarget

from .conftest import SAMPLE_EMPTY_HTML, SAMPLE_LISTING_HTML

class TestExtractItems:
    """Test the CSS selector item extraction logic."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    def test_basic_extraction(self, sample_target: ScrapingTarget):
        """Extracts all items with correct field values."""
        items = self.scraper._extract_items(SAMPLE_LISTING_HTML, sample_target)

        assert len(items) == 3
        assert items[0]["name"] == "Acme Corp"
        assert items[0]["country"] == "Germany"
        assert items[0]["city"] == "Berlin"
        assert items[0]["booth"] == "Hall 5, Stand A20"

        assert items[1]["name"] == "Beta Industries"
        assert items[1]["country"] == "France"
        assert items[1]["city"] == "Paris"

        assert items[2]["name"] == "Gamma Ltd"
        assert items[2]["country"] == "UK"

    def test_attribute_extraction(self, sample_target_with_attributes: ScrapingTarget):
        """Extracts values from HTML attributes instead of text content."""
        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target_with_attributes
        )

        assert len(items) == 3
        assert items[0]["logo_url"] == "/logos/acme.png"
        assert items[0]["detail_link"] == "/exhibitors/acme-corp"
        assert items[1]["logo_url"] == "/logos/beta.png"
        # Gamma has no logo
        assert items[2]["logo_url"] is None

    def test_detail_link_extraction(self, sample_target: ScrapingTarget):
        """Extracts detail_link from the detail_link_selector."""
        items = self.scraper._extract_items(SAMPLE_LISTING_HTML, sample_target)

        assert items[0]["detail_link"] == "/exhibitors/acme-corp"
        assert items[1]["detail_link"] == "/exhibitors/beta-industries"
        assert items[2]["detail_link"] == "/exhibitors/gamma-ltd"

    def test_api_id_extraction(self, sample_target: ScrapingTarget):
        """Extracts _detail_api_id using regex on attribute value."""
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}/profile",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"actionItem=(\d+)",
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )

        assert items[0]["_detail_api_id"] == "101"
        assert items[1]["_detail_api_id"] == "102"
        assert items[2]["_detail_api_id"] == "103"

    def test_empty_selector_skipped(self, sample_target_with_empty_selectors: ScrapingTarget):
        """Empty and whitespace-only CSS selectors are skipped gracefully."""
        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target_with_empty_selectors
        )

        assert len(items) == 3
        # Fields with empty selectors should be None
        assert items[0]["empty_field"] is None
        assert items[0]["whitespace_field"] is None
        # Valid fields should still work
        assert items[0]["name"] == "Acme Corp"
        assert items[0]["country"] == "Germany"

    def test_invalid_selector_handled(self):
        """Invalid CSS selectors don't crash — field is set to None."""
        target = ScrapingTarget(
            item_container_selector=".exhibitor-card",
            field_selectors={
                "name": "h3.company-name",
                "broken": "!!invalid!!selector",
            },
            field_attributes={},
        )

        items = self.scraper._extract_items(SAMPLE_LISTING_HTML, target)
        assert len(items) == 3
        assert items[0]["name"] == "Acme Corp"
        assert items[0]["broken"] is None

    def test_no_containers_found(self, sample_target: ScrapingTarget):
        """Returns empty list when no containers match."""
        items = self.scraper._extract_items(SAMPLE_EMPTY_HTML, sample_target)
        assert items == []

    def test_missing_field_returns_none(self):
        """Fields not found in a container are set to None."""
        target = ScrapingTarget(
            item_container_selector=".exhibitor-card",
            field_selectors={
                "name": "h3.company-name",
                "email": "a.email-link",  # Not in listing HTML
                "phone": "span.phone",    # Not in listing HTML
            },
            field_attributes={},
        )

        items = self.scraper._extract_items(SAMPLE_LISTING_HTML, target)
        assert len(items) == 3
        assert items[0]["name"] == "Acme Corp"
        assert items[0]["email"] is None
        assert items[0]["phone"] is None

    def test_api_id_no_match(self, sample_target: ScrapingTarget):
        """When regex doesn't match, _detail_api_id is None (not the raw attribute value).

        Setting _detail_api_id to None prevents constructing a broken API URL
        by substituting a raw unmatched attribute (e.g. '/hallplan?actionItem=101')
        into the API URL template placeholder.
        """
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"nonexistent=(\d+)",  # Won't match
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )

        # When regex is provided but doesn't match, the ID is None to avoid
        # substituting a garbage value into the API URL template.
        assert items[0].get("_detail_api_id") is None

    def test_detail_link_selector_empty(self):
        """Empty detail_link_selector is handled without error."""
        target = ScrapingTarget(
            item_container_selector=".exhibitor-card",
            field_selectors={"name": "h3.company-name"},
            field_attributes={},
            detail_link_selector="",
        )

        items = self.scraper._extract_items(SAMPLE_LISTING_HTML, target)
        assert len(items) == 3
        assert "detail_link" not in items[0]

    def test_api_id_empty_selector(self, sample_target: ScrapingTarget):
        """Empty id_selector in detail_api_plan is handled without error."""
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}",
            id_selector="",
            id_attribute="href",
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )
        assert len(items) == 3
        # No _detail_api_id should be set
        assert "_detail_api_id" not in items[0]


class TestResolvePageUrls:
    """Test the URL resolution logic."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    def test_no_pagination_urls(self, sample_plan):
        """Returns just the base URL when no pagination URLs are set."""
        urls = self.scraper._resolve_page_urls(sample_plan)
        assert urls == ["https://example.com/exhibitors"]

    def test_with_pagination_urls(self, sample_plan):
        """Returns pagination URLs when they are set."""
        sample_plan.pagination_urls = [
            "https://example.com/exhibitors?page=1",
            "https://example.com/exhibitors?page=2",
            "https://example.com/exhibitors?page=3",
        ]
        urls = self.scraper._resolve_page_urls(sample_plan)
        assert len(urls) == 3
        assert urls[0] == "https://example.com/exhibitors?page=1"


class TestDetailEnrichmentFiltering:
    """Tests for _enrich_detail_pages detail HTML filtering (Bug #2 fix)."""

    def setup_method(self):
        self.scraper = ScraperAgent()

    @pytest.mark.asyncio
    async def test_failed_fetches_excluded_from_detail_htmls(self, sample_plan):
        """Empty HTML strings from failed fetches must not be stored in detail_pages.

        Regression test for Bug #2: fetch_pages() returns "" for failed URLs,
        which the old code stored verbatim.  After the fix, only non-empty HTML
        is merged into detail_pages and fetched_urls.
        """
        from unittest.mock import AsyncMock, patch

        from app.models.schemas import PageData

        good_url = "https://example.com/exhibitors/acme-corp"
        bad_url = "https://example.com/exhibitors/beta-industries"

        items = [
            {"name": "Acme Corp", "detail_link": good_url},
            {"name": "Beta Industries", "detail_link": bad_url},
        ]
        pages = [PageData(url="https://example.com/exhibitors", items=items)]

        # Simulate fetch_pages returning "" for the bad URL (network failure)
        mock_batch_htmls = {
            good_url: "<html><body><p>Acme Corp Detail</p></body></html>",
            bad_url: "",  # failed fetch
        }

        with patch("app.agents.scraper_agent.fetch_pages", new_callable=AsyncMock, return_value=mock_batch_htmls):
            enrich_result = await self.scraper._enrich_detail_pages(pages, sample_plan)

        result_pages = enrich_result.pages
        all_detail_pages: dict[str, str] = {}
        for pd in result_pages:
            all_detail_pages.update(pd.detail_pages)

        # Only the successfully fetched URL should be in detail_pages
        assert good_url in all_detail_pages
        assert all_detail_pages[good_url]  # non-empty
        assert bad_url not in all_detail_pages, (
            f"Failed fetch (empty HTML) must not appear in detail_pages; "
            f"found {bad_url!r} with html={all_detail_pages.get(bad_url)!r}"
        )

        # fetched_urls should only include the successfully fetched URL
        assert good_url in enrich_result.fetched_urls
        assert bad_url not in enrich_result.fetched_urls

    def test_api_id_regex_no_match_returns_none(self, sample_target: ScrapingTarget):
        """Confirms the API ID is None when the regex doesn't match the attribute value.

        This is a regression test for Bug #3: previously the raw (unmatched)
        attribute value was stored, which would produce a broken API URL like
        https://example.com/api//hallplan?actionItem=101/profile.
        """
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}/profile",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"badpattern=(\d+)",  # deliberately won't match
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )

        # All items should have _detail_api_id == None (regex didn't match)
        for item in items:
            assert item.get("_detail_api_id") is None, (
                f"Expected None but got {item.get('_detail_api_id')!r}. "
                "A non-matching regex must not produce a garbage API ID."
            )

    def test_api_id_regex_match_returns_captured_group(self, sample_target: ScrapingTarget):
        """When the regex matches, only the capture group value is stored as the ID."""
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}/profile",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"actionItem=(\d+)",  # matches correctly
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )

        # The regex capture group should be the clean numeric ID
        assert items[0]["_detail_api_id"] == "101"
        assert items[1]["_detail_api_id"] == "102"
        assert items[2]["_detail_api_id"] == "103"
