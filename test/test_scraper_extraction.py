"""Tests for ScraperAgent._extract_items() and related extraction logic.

These tests validate the core CSS selector extraction without needing
network access or an LLM — they use pre-built HTML and ScrapingTargets.
"""

from __future__ import annotations

import pytest

from app.agents.scraper_agent import ScraperAgent
from app.models.schemas import DetailApiPlan, ScrapingTarget
from app.utils.structured_source import (
    detect_embedded_structured_source,
    extract_structured_items_from_html,
)

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

    def test_compound_api_id_extraction_from_floorplan_href(self, sample_target: ScrapingTarget):
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}/profile",
            id_selector="a.hall-map",
            id_attribute="href",
        )
        html = """
        <html><body>
        <div class="exhibitor-card">
          <h3 class="company-name">Acme Corp</h3>
          <span class="country">Germany</span>
          <span class="city">Berlin</span>
          <span class="booth">Hall 5</span>
          <a class="detail-link" href="/exhibitors/acme-corp">View Details</a>
          <a class="hall-map" href="/floorplan?action=showExhibitor&actionItem=3043124&_event=beauty2026">Map</a>
        </div>
        </body></html>
        """

        items = self.scraper._extract_items(html, sample_target, detail_api)

        assert items[0]["_detail_api_id"] == "beauty2026.3043124"

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
        """When regex doesn't match, _detail_api_id is None."""
        detail_api = DetailApiPlan(
            api_url_template="https://example.com/api/{id}",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"nonexistent=(\d+)",  # Won't match
        )

        items = self.scraper._extract_items(
            SAMPLE_LISTING_HTML, sample_target, detail_api
        )

        # ID should still be the raw href since regex didn't match
        # (the regex check only replaces if it matches)
        assert items[0].get("_detail_api_id") is not None

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


class TestStructuredSourceDetection:
    def test_detects_embedded_json_results_and_flattens_values(self):
        html = """
        <html><body>
          <wcl-ogz-data-source-hubdb-exhibitors data-info='{"results":[
            {"id":"1","path":"/acme","values":{"name":"Acme Corp","description":"Widget maker","website":"https://acme.com"}},
            {"id":"2","path":"/beta","values":{"name":"Beta GmbH","description":"Industrial parts","website":"https://beta.example"}}
          ]}'></wcl-ogz-data-source-hubdb-exhibitors>
        </body></html>
        """

        plan = detect_embedded_structured_source(html, source_url="https://example.com/exhibitors")

        assert plan is not None
        assert plan.source_kind == "embedded_html"
        assert plan.html_attribute == "data-info"
        assert plan.items_json_path == "results"
        assert plan.total_count == 2

        items = extract_structured_items_from_html(html, plan)
        assert len(items) == 2
        assert items[0]["id"] == "1"
        assert items[0]["path"] == "/acme"
        assert items[0]["values.name"] == "Acme Corp"
        assert items[0]["values.description"] == "Widget maker"
        assert items[0]["values.website"] == "https://acme.com"
