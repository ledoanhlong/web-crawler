"""conftest.py — Shared test fixtures for the web crawler test suite."""

from __future__ import annotations

import pytest

from app.models.schemas import (
    CrawlJob,
    CrawlRequest,
    DetailApiPlan,
    DetailPagePlan,
    ExtractionMethod,
    PageData,
    PaginationStrategy,
    ScrapingPlan,
    ScrapingTarget,
    SellerLead,
)


# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

SAMPLE_LISTING_HTML = """
<html>
<body>
<div id="exhibitor-list">
  <div class="exhibitor-card" data-id="101">
    <h3 class="company-name">Acme Corp</h3>
    <span class="country">Germany</span>
    <span class="city">Berlin</span>
    <span class="booth">Hall 5, Stand A20</span>
    <a class="detail-link" href="/exhibitors/acme-corp">View Details</a>
    <a class="hall-map" href="/hallplan?actionItem=101">Map</a>
    <img class="logo" src="/logos/acme.png">
  </div>
  <div class="exhibitor-card" data-id="102">
    <h3 class="company-name">Beta Industries</h3>
    <span class="country">France</span>
    <span class="city">Paris</span>
    <span class="booth">Hall 3, Stand B10</span>
    <a class="detail-link" href="/exhibitors/beta-industries">View Details</a>
    <a class="hall-map" href="/hallplan?actionItem=102">Map</a>
    <img class="logo" src="/logos/beta.png">
  </div>
  <div class="exhibitor-card" data-id="103">
    <h3 class="company-name">Gamma Ltd</h3>
    <span class="country">UK</span>
    <span class="city">London</span>
    <span class="booth">Hall 1, Stand C05</span>
    <a class="detail-link" href="/exhibitors/gamma-ltd">View Details</a>
    <a class="hall-map" href="/hallplan?actionItem=103">Map</a>
  </div>
</div>
</body>
</html>
"""

SAMPLE_DETAIL_HTML = """
<html>
<body>
<div class="profile">
  <h1 class="company-title">Acme Corp</h1>
  <div class="contact">
    <div class="address">123 Main St, 10115 Berlin, Germany</div>
    <div class="phone">+49 30 1234567</div>
    <a class="email-link" href="mailto:info@acme.com">info@acme.com</a>
    <a class="website-link" href="https://www.acme.com">www.acme.com</a>
  </div>
  <div class="description">
    Leading manufacturer of industrial widgets since 1985.
  </div>
  <div class="products">
    <ul>
      <li>Widgets</li>
      <li>Gadgets</li>
      <li>Tools</li>
    </ul>
  </div>
  <div class="social">
    <a href="https://facebook.com/acmecorp">Facebook</a>
    <a href="https://linkedin.com/company/acmecorp">LinkedIn</a>
  </div>
</div>
</body>
</html>
"""

SAMPLE_EMPTY_HTML = "<html><body><p>No exhibitors found.</p></body></html>"


# ---------------------------------------------------------------------------
# ScrapingPlan fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_target() -> ScrapingTarget:
    """A sample ScrapingTarget for testing."""
    return ScrapingTarget(
        item_container_selector=".exhibitor-card",
        field_selectors={
            "name": "h3.company-name",
            "country": "span.country",
            "city": "span.city",
            "booth": "span.booth",
        },
        field_attributes={},
        detail_link_selector="a.detail-link",
    )


@pytest.fixture
def sample_target_with_attributes() -> ScrapingTarget:
    """A ScrapingTarget that uses attributes for some fields."""
    return ScrapingTarget(
        item_container_selector=".exhibitor-card",
        field_selectors={
            "name": "h3.company-name",
            "country": "span.country",
            "logo_url": "img.logo",
            "detail_link": "a.detail-link",
        },
        field_attributes={
            "logo_url": "src",
            "detail_link": "href",
        },
        detail_link_selector="a.detail-link",
    )


@pytest.fixture
def sample_target_with_empty_selectors() -> ScrapingTarget:
    """A ScrapingTarget with empty/whitespace selectors (edge case)."""
    return ScrapingTarget(
        item_container_selector=".exhibitor-card",
        field_selectors={
            "name": "h3.company-name",
            "empty_field": "",
            "whitespace_field": "   ",
            "country": "span.country",
        },
        field_attributes={},
    )


@pytest.fixture
def sample_plan(sample_target: ScrapingTarget) -> ScrapingPlan:
    """A basic ScrapingPlan for testing."""
    return ScrapingPlan(
        url="https://example.com/exhibitors",
        requires_javascript=False,
        pagination=PaginationStrategy.NONE,
        target=sample_target,
    )


@pytest.fixture
def sample_plan_with_detail(sample_target: ScrapingTarget) -> ScrapingPlan:
    """A ScrapingPlan with detail page analysis."""
    return ScrapingPlan(
        url="https://example.com/exhibitors",
        requires_javascript=False,
        pagination=PaginationStrategy.NONE,
        target=sample_target,
        detail_page_plan=DetailPagePlan(
            field_selectors={
                "address": ".contact .address",
                "phone": ".contact .phone",
                "email": "a.email-link",
                "website": "a.website-link",
                "description": ".description",
            },
            field_attributes={
                "email": "href",
                "website": "href",
            },
        ),
    )


@pytest.fixture
def sample_plan_with_api(sample_target: ScrapingTarget) -> ScrapingPlan:
    """A ScrapingPlan with API interception detail plan."""
    return ScrapingPlan(
        url="https://example.com/exhibitors",
        requires_javascript=True,
        pagination=PaginationStrategy.ALPHABET_TABS,
        alphabet_tab_selector=".letter-tab",
        target=sample_target,
        detail_api_plan=DetailApiPlan(
            api_url_template="https://example.com/api/exhibitors/{id}/profile",
            id_selector="a.hall-map",
            id_attribute="href",
            id_regex=r"actionItem=(\d+)",
        ),
    )


@pytest.fixture
def sample_page_data() -> PageData:
    """A PageData object with sample items."""
    return PageData(
        url="https://example.com/exhibitors",
        items=[
            {
                "name": "Acme Corp",
                "country": "Germany",
                "city": "Berlin",
                "booth": "Hall 5, Stand A20",
            },
            {
                "name": "Beta Industries",
                "country": "France",
                "city": "Paris",
                "booth": "Hall 3, Stand B10",
            },
        ],
    )


@pytest.fixture
def sample_seller_lead() -> SellerLead:
    """A sample SellerLead record."""
    return SellerLead(
        name="Acme Corp",
        country="Germany",
        city="Berlin",
        address="123 Main St, 10115 Berlin",
        website="https://www.acme.com",
        email="info@acme.com",
        phone="+49 30 1234567",
        description="Leading manufacturer of industrial widgets since 1985.",
        product_categories=["Widgets", "Gadgets", "Tools"],
    )


@pytest.fixture
def sample_crawl_request() -> CrawlRequest:
    """A CrawlRequest for testing."""
    return CrawlRequest(
        url="https://example.com/exhibitors",
        fields_wanted="name, country, city, email, phone, website",
    )


@pytest.fixture
def sample_crawl_job(sample_crawl_request: CrawlRequest) -> CrawlJob:
    """A CrawlJob in pending state."""
    return CrawlJob(request=sample_crawl_request)
