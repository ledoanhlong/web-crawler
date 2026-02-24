from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Scraping plan (output of PlannerAgent)
# ---------------------------------------------------------------------------
class PaginationStrategy(str, enum.Enum):
    NONE = "none"
    NEXT_BUTTON = "next_button"
    PAGE_NUMBERS = "page_numbers"
    INFINITE_SCROLL = "infinite_scroll"
    LOAD_MORE_BUTTON = "load_more_button"
    ALPHABET_TABS = "alphabet_tabs"
    API_ENDPOINT = "api_endpoint"


class ScrapingTarget(BaseModel):
    """Describes how to locate and extract items on a single listing page."""

    item_container_selector: str = Field(
        description="CSS selector for the repeating container that wraps each exhibitor/seller."
    )
    field_selectors: dict[str, str] = Field(
        description=(
            "Map of field name -> CSS selector (relative to item container). "
            "E.g. {'name': 'h3.company-name', 'booth': 'span.booth-nr'}."
        )
    )
    field_attributes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of field name -> HTML attribute to read instead of text content. "
            "E.g. {'website': 'href', 'logo': 'src'}."
        ),
    )
    detail_link_selector: str | None = Field(
        default=None,
        description="CSS selector for the link to an exhibitor's detail page (relative to item container).",
    )


class ScrapingPlan(BaseModel):
    """Full plan produced by the PlannerAgent for a given URL."""

    url: str
    requires_javascript: bool = Field(
        description="Whether Playwright is needed (SPA, dynamic rendering, hash routing, etc.)."
    )
    pagination: PaginationStrategy
    pagination_selector: str | None = Field(
        default=None,
        description="CSS selector for the pagination control (next button, load-more, page links, etc.).",
    )
    pagination_urls: list[str] = Field(
        default_factory=list,
        description="Pre-computed list of paginated URLs when the pattern is predictable.",
    )
    alphabet_tab_selector: str | None = Field(
        default=None,
        description="CSS selector for alphabet/category tabs if pagination is ALPHABET_TABS.",
    )
    api_endpoint: str | None = Field(
        default=None,
        description="Discovered JSON API endpoint if the site fetches data via XHR.",
    )
    api_params: dict[str, str] = Field(
        default_factory=dict,
        description="Query parameters template for the API endpoint.",
    )
    target: ScrapingTarget
    detail_page_fields: dict[str, str] = Field(
        default_factory=dict,
        description="Additional field selectors to extract from each exhibitor's detail page.",
    )
    detail_page_field_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Attribute overrides for detail page fields.",
    )
    wait_selector: str | None = Field(
        default=None,
        description="CSS selector to wait for before scraping (useful for JS-rendered pages).",
    )
    notes: str = Field(
        default="",
        description="Free-form notes from the planner about edge cases or things to watch for.",
    )


# ---------------------------------------------------------------------------
# Raw page data (output of ScraperAgent)
# ---------------------------------------------------------------------------
class PageData(BaseModel):
    """Raw data scraped from a single page."""

    url: str
    items: list[dict[str, str | None]] = Field(
        description="List of raw field dicts extracted from the listing page."
    )
    detail_pages: dict[str, str] = Field(
        default_factory=dict,
        description="Map of detail-page URL -> raw HTML (or extracted text) for enrichment.",
    )


# ---------------------------------------------------------------------------
# Parsed exhibitor record (output of ParserAgent)
# ---------------------------------------------------------------------------
class ExhibitorRecord(BaseModel):
    """Normalized exhibitor / seller record."""

    name: str
    booth_or_stand: str | None = None
    country: str | None = None
    city: str | None = None
    address: str | None = None
    postal_code: str | None = None
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    fax: str | None = None
    description: str | None = None
    product_categories: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    hall: str | None = None
    logo_url: str | None = None
    social_media: dict[str, str] = Field(default_factory=dict)
    raw_extra: dict[str, str] = Field(
        default_factory=dict,
        description="Any additional fields that didn't map to the standard schema.",
    )
    source_url: str | None = None


# ---------------------------------------------------------------------------
# Crawl job lifecycle
# ---------------------------------------------------------------------------
class CrawlStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    SCRAPING = "scraping"
    PARSING = "parsing"
    OUTPUT = "output"
    COMPLETED = "completed"
    FAILED = "failed"


class CrawlRequest(BaseModel):
    url: str = Field(description="The exhibitor listing URL to crawl.")
    max_pages: int | None = Field(
        default=None, description="Override the default max pages for this crawl."
    )


class CrawlResult(BaseModel):
    records: list[ExhibitorRecord] = Field(default_factory=list)
    json_path: str | None = None
    csv_path: str | None = None


class CrawlJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: CrawlRequest
    status: CrawlStatus = CrawlStatus.PENDING
    plan: ScrapingPlan | None = None
    result: CrawlResult | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
