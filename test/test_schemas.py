"""Tests for Pydantic schemas and model validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import (
    ConfirmPreviewRequest,
    CrawlJob,
    CrawlRequest,
    CrawlResult,
    CrawlStatus,
    DetailApiPlan,
    DetailPagePlan,
    DetailSubLink,
    ExtractionMethod,
    FailureCategory,
    FailureEvent,
    JobDiagnostics,
    ListingApiPlan,
    PageData,
    PaginationStrategy,
    PipelineStage,
    ScrapingPlan,
    ScrapingTarget,
    ScrapingTemplate,
    SellerLead,
    StageConfidence,
    SmartCrawlRequest,
)


class TestCrawlRequest:
    """Test CrawlRequest URL validation."""

    def test_valid_url(self):
        req = CrawlRequest(url="https://example.com/exhibitors")
        assert req.url == "https://example.com/exhibitors"

    def test_valid_url_with_path(self):
        req = CrawlRequest(url="https://www.example.com/dir/?page=1&q=test")
        assert req.url.startswith("https://")

    def test_http_url_accepted(self):
        req = CrawlRequest(url="http://example.com/list")
        assert req.url == "http://example.com/list"

    def test_empty_url_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest(url="")

    def test_whitespace_url_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest(url="   ")

    def test_ftp_url_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest(url="ftp://example.com/file")

    def test_no_domain_rejected(self):
        with pytest.raises(ValidationError):
            CrawlRequest(url="https://")

    def test_url_stripped(self):
        req = CrawlRequest(url="  https://example.com  ")
        assert req.url == "https://example.com"

    def test_optional_fields_default(self):
        req = CrawlRequest(url="https://example.com")
        assert req.detail_page_url is None
        assert req.fields_wanted is None
        assert req.test_single is False
        assert req.max_items is None
        assert req.page_type is None
        assert req.rendering_type is None
        assert req.detail_page_type is None


class TestScrapingPlan:
    """Test ScrapingPlan construction and validation."""

    def test_minimal_plan(self):
        plan = ScrapingPlan(
            url="https://example.com",
            requires_javascript=False,
            pagination=PaginationStrategy.NONE,
            target=ScrapingTarget(
                item_container_selector=".item",
                field_selectors={"name": "h3"},
            ),
        )
        assert plan.url == "https://example.com"
        assert plan.requires_javascript is False
        assert plan.pagination == PaginationStrategy.NONE

    def test_pagination_strategies(self):
        """All pagination strategies are valid enum values."""
        for strategy in PaginationStrategy:
            plan = ScrapingPlan(
                url="https://example.com",
                requires_javascript=False,
                pagination=strategy,
                target=ScrapingTarget(
                    item_container_selector=".item",
                    field_selectors={"name": "h3"},
                ),
            )
            assert plan.pagination == strategy

    def test_plan_with_detail_page_plan(self):
        plan = ScrapingPlan(
            url="https://example.com",
            requires_javascript=False,
            pagination=PaginationStrategy.NONE,
            target=ScrapingTarget(
                item_container_selector=".item",
                field_selectors={"name": "h3"},
                detail_link_selector="a.detail",
            ),
            detail_page_plan=DetailPagePlan(
                field_selectors={"email": "a.email", "phone": ".phone"},
                field_attributes={"email": "href"},
                sub_links=[
                    DetailSubLink(label="Products", selector="a.products-tab"),
                ],
            ),
        )
        assert plan.detail_page_plan is not None
        assert len(plan.detail_page_plan.field_selectors) == 2
        assert len(plan.detail_page_plan.sub_links) == 1

    def test_plan_with_detail_api_plan(self):
        plan = ScrapingPlan(
            url="https://example.com",
            requires_javascript=True,
            pagination=PaginationStrategy.ALPHABET_TABS,
            alphabet_tab_selector=".letter",
            target=ScrapingTarget(
                item_container_selector=".item",
                field_selectors={"name": "h3"},
                detail_button_selector="button.details",
            ),
            detail_api_plan=DetailApiPlan(
                api_url_template="https://example.com/api/{id}/profile",
                id_selector="a[data-id]",
                id_attribute="data-id",
            ),
        )
        assert plan.detail_api_plan is not None
        assert "{id}" in plan.detail_api_plan.api_url_template

    def test_plan_defaults(self):
        plan = ScrapingPlan(
            url="https://example.com",
            requires_javascript=False,
            pagination=PaginationStrategy.NONE,
            target=ScrapingTarget(
                item_container_selector=".item",
                field_selectors={},
            ),
        )
        assert plan.pagination_urls == []
        assert plan.detail_page_fields == {}
        assert plan.detail_page_plan is None
        assert plan.detail_api_plan is None
        assert plan.wait_selector is None
        assert plan.notes == ""

    def test_plan_with_embedded_structured_source(self):
        plan = ScrapingPlan(
            url="https://example.com",
            requires_javascript=False,
            pagination=PaginationStrategy.NONE,
            target=ScrapingTarget(
                item_container_selector=".item",
                field_selectors={"name": "h3"},
            ),
            listing_api_plan=ListingApiPlan(
                source_kind="embedded_html",
                api_url="https://example.com",
                html_selector="custom-directory[data-info]",
                html_attribute="data-info",
                items_json_path="results",
                total_count=42,
            ),
        )
        assert plan.listing_api_plan is not None
        assert plan.listing_api_plan.source_kind == "embedded_html"
        assert plan.listing_api_plan.html_selector == "custom-directory[data-info]"


class TestSellerLead:
    """Test SellerLead model."""

    def test_minimal_seller_lead(self):
        lead = SellerLead(name="Acme Corp")
        assert lead.name == "Acme Corp"
        assert lead.country is None
        assert lead.email is None
        assert lead.product_categories == []
        assert lead.social_media == {}

    def test_full_seller_lead(self):
        lead = SellerLead(
            name="Acme Corp",
            country="Germany",
            city="Berlin",
            address="123 Main St",
            postal_code="10115",
            website="https://acme.com",
            email="info@acme.com",
            phone="+49 30 1234567",
            description="Widget maker",
            product_categories=["Widgets", "Tools"],
            brands=["AcmeBrand"],
            marketplace_name="BEAUTY Dusseldorf",
            logo_url="https://example.com/logo.png",
            social_media={"facebook": "https://fb.com/acme"},
            raw_extra={"hall": "5"},
            source_url="https://example.com/exhibitors",
        )
        assert lead.name == "Acme Corp"
        assert len(lead.product_categories) == 2
        assert "facebook" in lead.social_media


class TestPageData:
    """Test PageData model."""

    def test_basic_page_data(self):
        pd = PageData(
            url="https://example.com",
            items=[{"name": "Test", "country": "US"}],
        )
        assert pd.url == "https://example.com"
        assert len(pd.items) == 1
        assert pd.detail_pages == {}
        assert pd.detail_api_responses == {}

    def test_page_data_with_details(self):
        pd = PageData(
            url="https://example.com",
            items=[{"name": "Test"}],
            detail_pages={"https://example.com/detail/1": "<html>...</html>"},
            detail_api_responses={"123": {"name": "Test", "email": "test@test.com"}},
        )
        assert len(pd.detail_pages) == 1
        assert len(pd.detail_api_responses) == 1


class TestCrawlJob:
    """Test CrawlJob lifecycle."""

    def test_job_creation(self):
        job = CrawlJob(request=CrawlRequest(url="https://example.com"))
        assert len(job.id) == 12
        assert job.status == CrawlStatus.PENDING
        assert job.plan is None
        assert job.result is None
        assert job.error is None
        assert isinstance(job.diagnostics, JobDiagnostics)
        assert job.diagnostics.failures == []
        assert job.diagnostics.stage_confidences == []
        assert job.diagnostics.status_timeline == []

    def test_job_id_uniqueness(self):
        job1 = CrawlJob(request=CrawlRequest(url="https://example.com"))
        job2 = CrawlJob(request=CrawlRequest(url="https://example.com"))
        assert job1.id != job2.id

    def test_job_timestamps_utc(self):
        job = CrawlJob(request=CrawlRequest(url="https://example.com"))
        assert job.created_at.tzinfo is not None
        assert job.updated_at.tzinfo is not None

    def test_confirm_preview_request(self):
        req = ConfirmPreviewRequest(approved=True, feedback="Looks great")
        assert req.approved is True
        assert req.feedback == "Looks great"
        assert req.extraction_method is None

    def test_confirm_with_method(self):
        req = ConfirmPreviewRequest(
            approved=True,
            extraction_method=ExtractionMethod.CSS,
        )
        assert req.extraction_method == ExtractionMethod.CSS


class TestSmartCrawlRequest:
    """Test SmartCrawlRequest model."""

    def test_basic_request(self):
        req = SmartCrawlRequest(
            urls=["https://example.com"],
            prompt="Extract names",
        )
        assert len(req.urls) == 1
        assert req.test_single is False
        assert req.page_type is None
        assert req.rendering_type is None
        assert req.detail_page_type is None

    def test_request_with_test_single(self):
        req = SmartCrawlRequest(
            urls=["https://example.com"],
            prompt="Extract names",
            test_single=True,
        )
        assert req.test_single is True

    def test_request_with_page_options(self):
        req = SmartCrawlRequest(
            urls=["https://example.com"],
            prompt="Extract names",
            page_type="directory",
            rendering_type="dynamic",
            detail_page_type="separate_page",
        )
        assert req.page_type == "directory"
        assert req.rendering_type == "dynamic"
        assert req.detail_page_type == "separate_page"


class TestExtractionMethod:
    """Test ExtractionMethod enum."""

    def test_css_value(self):
        assert ExtractionMethod.CSS.value == "css"

    def test_smart_scraper_value(self):
        assert ExtractionMethod.SMART_SCRAPER.value == "smart_scraper"

    def test_crawl4ai_value(self):
        assert ExtractionMethod.CRAWL4AI.value == "crawl4ai"

    def test_universal_scraper_value(self):
        assert ExtractionMethod.UNIVERSAL_SCRAPER.value == "universal_scraper"

    def test_all_methods_have_unique_values(self):
        values = [m.value for m in ExtractionMethod]
        assert len(values) == len(set(values))


class TestReliabilityDiagnostics:
    """Test reliability diagnostics models."""

    def test_stage_confidence_bounds(self):
        confidence = StageConfidence(
            stage=PipelineStage.SCRAPING,
            score=0.75,
            reason="Good extraction signal",
        )
        assert confidence.score == 0.75

        with pytest.raises(ValidationError):
            StageConfidence(stage=PipelineStage.SCRAPING, score=1.2)

    def test_failure_event_defaults(self):
        event = FailureEvent(
            category=FailureCategory.NETWORK_TRANSIENT,
            stage=PipelineStage.SCRAPING,
            message="Timeout while fetching page",
        )
        assert event.retryable is True
        assert event.details == {}
