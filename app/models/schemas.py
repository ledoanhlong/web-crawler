from __future__ import annotations

import enum
import ipaddress
import socket
import uuid
from datetime import datetime, timezone

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


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
        description="CSS selector for the repeating container that wraps each item (seller, company, item)."
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
        description="CSS selector for the link to an item's detail page (relative to item container).",
    )
    detail_button_selector: str | None = Field(
        default=None,
        description="CSS selector for a JS-only detail button with no href (relative to item container).",
    )


class DetailSubLink(BaseModel):
    """A link on a detail page that may lead to additional useful data."""

    label: str = Field(
        description="The visible text or inferred purpose of the link (e.g. 'Products', 'Contact')."
    )
    selector: str = Field(
        description="CSS selector to find this link on the detail page."
    )
    attribute: str = Field(
        default="href",
        description="HTML attribute to read the URL from.",
    )


class DetailPagePlan(BaseModel):
    """Analysis result for a sample detail page, produced by the PlannerAgent."""

    field_selectors: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Map of field name -> CSS selector for extracting data from the detail page. "
            "E.g. {'email': 'a.email-link', 'phone': 'span.phone'}."
        ),
    )
    field_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Map of field name -> HTML attribute to read instead of text content.",
    )
    sub_links: list[DetailSubLink] = Field(
        default_factory=list,
        description=(
            "Links on the detail page worth following for additional data "
            "(e.g. 'Products' tab, 'Contact' page, 'About us')."
        ),
    )


class DetailApiPlan(BaseModel):
    """Describes how to fetch detail data from a discovered JSON API endpoint."""

    api_url_template: str = Field(
        description=(
            "URL template with {id} placeholder. "
            "E.g. 'https://example.com/api/sellers/{id}/profile'"
        )
    )
    id_selector: str = Field(
        description="CSS selector (relative to item container) for the element containing the item ID.",
    )
    id_attribute: str | None = Field(
        default=None,
        description="HTML attribute to read the ID from (e.g. 'data-id'). If null, use text content.",
    )
    id_regex: str | None = Field(
        default=None,
        description="Optional regex with one capture group to extract the ID from the attribute value.",
    )
    sample_response: dict | None = Field(
        default=None,
        description="A sample JSON response from the API (stored for parser context).",
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
    inner_pagination_selector: str | None = Field(
        default=None,
        description=(
            "CSS selector for secondary/inner numbered page links "
            "(e.g. numbered pagination within an alphabet tab or filter view)."
        ),
    )
    total_items_hint: int | None = Field(
        default=None,
        description="Estimated total number of items if a count is visible on the page.",
    )
    api_endpoint: str | None = Field(
        default=None,
        description="Discovered JSON API endpoint if the site fetches data via XHR.",
    )
    api_params: dict[str, str] = Field(
        default_factory=dict,
        description="Query parameters template for the API endpoint.",
    )
    api_page_param: str = Field(
        default="page",
        description="Name of the pagination query parameter (e.g. 'page', 'p', 'offset', 'start', 'pageNumber').",
    )
    api_page_start: int = Field(
        default=0,
        description="First page index — 0 for zero-indexed APIs, 1 for one-indexed APIs.",
    )
    target: ScrapingTarget
    detail_page_fields: dict[str, str] = Field(
        default_factory=dict,
        description="Additional field selectors to extract from each item's detail page.",
    )
    detail_page_field_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Attribute overrides for detail page fields.",
    )
    detail_page_plan: DetailPagePlan | None = Field(
        default=None,
        description="Analysis of a sample detail page (produced by planner after fetching one detail page).",
    )
    detail_api_plan: DetailApiPlan | None = Field(
        default=None,
        description="API-based detail enrichment plan (when details are loaded via XHR, not page navigation).",
    )
    wait_selector: str | None = Field(
        default=None,
        description="CSS selector to wait for before scraping (useful for JS-rendered pages).",
    )
    notes: str = Field(
        default="",
        description="Free-form notes from the planner about edge cases or things to watch for.",
    )
    selector_metrics: dict[str, float | int] = Field(
        default_factory=dict,
        description="Preflight selector quality metrics computed against sample HTML.",
    )


# ---------------------------------------------------------------------------
# Template hints (structural pattern — no CSS selectors)
# ---------------------------------------------------------------------------
class TemplateHints(BaseModel):
    """Structural hints describing a website pattern.

    These guide the planner agent without prescribing CSS selectors,
    making templates reusable across any website that follows the pattern.
    """

    requires_javascript: bool = Field(
        default=True,
        description="Whether the page needs Playwright (JS-rendered SPA).",
    )
    pagination: str = Field(
        default="none",
        description="Weak hint for expected pagination. Ignored when the user specifies pagination_type.",
    )
    has_detail_pages: bool = Field(
        default=False,
        description="Whether each listing item links to a separate detail page.",
    )
    has_detail_api: bool = Field(
        default=False,
        description="Whether detail data is loaded via XHR/API (not page navigation).",
    )
    notes: str = Field(
        default="",
        description="Free-form guidance for the planner about the website pattern.",
    )


# ---------------------------------------------------------------------------
# Scraping template (website pattern — reusable across sites)
# ---------------------------------------------------------------------------
class ScrapingTemplate(BaseModel):
    """A reusable scraping template describing a website pattern.

    Templates provide structural hints (JS need, pagination type, detail page
    strategy) that guide the planner agent.  They do NOT contain CSS selectors
    — the planner generates those by analysing the actual target page.
    """

    id: str = Field(description="Unique template identifier (matches filename).")
    name: str = Field(description="Human-readable template name.")
    description: str = Field(description="What this template pattern looks like.")
    platform: str = Field(default="", description="Platform/CMS identifier for grouping.")
    default_prompt: str = Field(default="", description="Default extraction prompt.")
    default_fields_wanted: str = Field(default="", description="Default fields list.")
    hints: TemplateHints = Field(description="Structural hints for the planner agent.")


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
    detail_sub_pages: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Map of detail-page URL -> {sub_link_label: sub_page_HTML}. "
            "Contains HTML from followed sub-links on each detail page."
        ),
    )
    detail_api_responses: dict[str, dict] = Field(
        default_factory=dict,
        description="Map of item ID -> parsed JSON response from the detail API.",
    )
    structured_data: dict = Field(
        default_factory=dict,
        description="JSON-LD, Open Graph, and Microdata extracted from the page HTML.",
    )


# ---------------------------------------------------------------------------
# Parsed seller lead record (output of ParserAgent)
# ---------------------------------------------------------------------------
class SellerLead(BaseModel):
    """Normalized company / seller lead record."""

    name: str
    country: str | None = None
    city: str | None = None
    address: str | None = None
    postal_code: str | None = None
    website: str | None = None
    store_url: str | None = Field(
        default=None,
        description="Seller's marketplace storefront URL (e.g. Amazon seller page).",
    )
    email: str | None = None
    phone: str | None = None
    description: str | None = None
    product_categories: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)
    marketplace_name: str | None = Field(
        default=None,
        description="Name of the marketplace, directory, or platform this lead was found on.",
    )
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
class ExtractionMethod(str, enum.Enum):
    CSS = "css"
    SMART_SCRAPER = "smart_scraper"
    CRAWL4AI = "crawl4ai"
    UNIVERSAL_SCRAPER = "universal_scraper"


class CrawlStatus(str, enum.Enum):
    PENDING = "pending"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    SCRAPING = "scraping"
    PREVIEW = "preview"
    PARSING = "parsing"
    OUTPUT = "output"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class PipelineStage(str, enum.Enum):
    PLANNING = "planning"
    SCRAPING = "scraping"
    PARSING = "parsing"
    OUTPUT = "output"


class FailureCategory(str, enum.Enum):
    NETWORK_TRANSIENT = "network_transient"
    ANTI_BOT = "anti_bot"
    RENDERING = "rendering"
    SELECTOR_MISMATCH = "selector_mismatch"
    PAGINATION_MISMATCH = "pagination_mismatch"
    DETAIL_ENRICHMENT = "detail_enrichment"
    PARSER_SCHEMA_MISMATCH = "parser_schema_mismatch"
    QUALITY_THRESHOLD = "quality_threshold"
    UNKNOWN = "unknown"


class StageConfidence(BaseModel):
    stage: PipelineStage
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="")
    measured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class FailureEvent(BaseModel):
    category: FailureCategory
    stage: PipelineStage
    message: str
    retryable: bool = True
    details: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobDiagnostics(BaseModel):
    counters: dict[str, int] = Field(
        default_factory=lambda: {
            "scrape_attempts": 0,
            "method_switches": 0,
            "pages_processed": 0,
            "empty_pages": 0,
            "items_extracted": 0,
            "detail_pages_fetched": 0,
            "detail_pages_remaining": 0,
            "parser_non_empty_fields": 0,
            "parser_total_fields": 0,
            "parser_structured_fields": 0,
        },
    )
    stage_confidences: list[StageConfidence] = Field(default_factory=list)
    failures: list[FailureEvent] = Field(default_factory=list)
    status_timeline: list[str] = Field(default_factory=list)
    parser_metrics: dict[str, float | int] = Field(
        default_factory=lambda: {
            "record_count": 0,
            "non_empty_fields": 0,
            "total_fields": 0,
            "structured_non_empty": 0,
            "structured_total": 0,
            "name_present": 0,
            "scalar_ratio": 0.0,
            "structured_ratio": 0.0,
            "name_ratio": 0.0,
        },
        description="Parser completeness metrics used to compute parsing-stage confidence.",
    )


def _reject_private_url(hostname: str | None) -> None:
    """Raise ValueError if the hostname resolves to a private/reserved IP.

    Prevents SSRF attacks targeting cloud metadata endpoints (169.254.x.x),
    internal services (localhost, 10.x.x.x, 192.168.x.x), etc.
    """
    if not hostname:
        return
    # Check for obvious private hostnames
    if hostname.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise ValueError(f"URL must not target localhost or loopback addresses")
    try:
        # Try to parse as IP directly first
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"URL must not target private or reserved IP addresses")
    except ValueError as exc:
        if "private" in str(exc).lower() or "reserved" in str(exc).lower() or "localhost" in str(exc).lower():
            raise
        # Not a direct IP — resolve the hostname
        try:
            resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _, _, _, _, addr in resolved:
                ip = ipaddress.ip_address(addr[0])
                if ip.is_private or ip.is_reserved or ip.is_loopback or ip.is_link_local:
                    raise ValueError(
                        f"URL hostname '{hostname}' resolves to a private/reserved IP ({addr[0]})"
                    )
        except socket.gaierror:
            pass  # DNS resolution failed — let the actual HTTP request handle it


class CrawlRequest(BaseModel):
    url: str = Field(description="The listing page URL to crawl (directory, marketplace, brand page, etc.).")
    detail_page_url: str | None = Field(
        default=None,
        description=(
            "An example detail/profile page URL so the planner can analyse its structure. "
            "E.g. click on one company or seller and paste that URL here."
        ),
    )
    fields_wanted: str | None = Field(
        default=None,
        description=(
            "Comma-separated list of fields the user wants extracted. "
            "E.g. 'name, booth, country, city, email, phone, website, description'."
        ),
    )
    item_description: str | None = Field(
        default=None,
        description=(
            "Describes what each item on the page represents and looks like. "
            "E.g. 'exhibitor card with company name, logo, and booth number' or "
            "'product listing with title, price, and rating'."
        ),
    )
    site_notes: str | None = Field(
        default=None,
        description=(
            "Any observations about the site that might help scraping. "
            "E.g. 'items load via AJAX after clicking a tab', 'site is behind Cloudflare', "
            "'pagination is at the bottom with page numbers 1-80'."
        ),
    )
    pagination_type: str | None = Field(
        default=None,
        description=(
            "User-specified pagination strategy. One of: "
            "'none' (single page), 'infinite_scroll', 'load_more_button', "
            "'next_button', 'page_numbers', 'alphabet_tabs'. "
            "If omitted the planner will auto-detect."
        ),
    )
    test_single: bool = Field(
        default=False,
        description="If true, only scrape and output a single item for testing.",
    )
    max_items: int | None = Field(
        default=None,
        description="Maximum number of items to scrape (for testing). None means no limit.",
    )
    max_pages: int | None = Field(
        default=None,
        description="Maximum number of pages to scrape. None means use global default.",
    )
    page_type: str | None = Field(
        default=None,
        description=(
            "What kind of page: 'directory', 'product_listing', 'event', or None for auto."
        ),
    )
    rendering_type: str | None = Field(
        default=None,
        description=(
            "How the page is built: 'static', 'dynamic', or None for auto-detect."
        ),
    )
    detail_page_type: str | None = Field(
        default=None,
        description=(
            "Detail page behavior: 'separate_page', 'none', 'popup_overlay', or None for auto-detect."
        ),
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL must not be empty")
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"URL must start with http:// or https:// (got {parsed.scheme!r})"
            )
        if not parsed.netloc:
            raise ValueError("URL must include a valid domain (e.g. https://example.com)")
        _reject_private_url(parsed.hostname)
        return v


class ConfirmPreviewRequest(BaseModel):
    """Sent by the user after reviewing a preview record."""
    approved: bool = Field(description="True to continue the full crawl, false to abort.")
    feedback: str | None = Field(
        default=None,
        description="Optional user feedback describing what data is missing or wrong.",
    )
    extraction_method: ExtractionMethod | None = Field(
        default=None,
        description="User's chosen extraction method for the full crawl (css, smart_scraper, crawl4ai, or universal_scraper).",
    )


class UpdatePlanRequest(BaseModel):
    """Sent by the user to edit plan fields during PLAN_REVIEW."""
    pagination: PaginationStrategy | None = Field(
        default=None,
        description="Override detected pagination strategy.",
    )
    requires_javascript: bool | None = Field(
        default=None,
        description="Override JS rendering requirement.",
    )
    item_container_selector: str | None = Field(
        default=None,
        description="Override the CSS selector for item containers.",
    )
    detail_link_selector: str | None = Field(
        default=None,
        description="Override the detail link CSS selector.",
    )
    max_pages: int | None = Field(
        default=None,
        description="Override the max pages to scrape.",
    )
    feedback: str | None = Field(
        default=None,
        description="Free-text feedback to trigger LLM re-analysis.",
    )


class CrawlResult(BaseModel):
    records: list[SellerLead] = Field(default_factory=list)
    json_path: str | None = None
    csv_path: str | None = None


class CrawlJob(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    request: CrawlRequest
    status: CrawlStatus = CrawlStatus.PENDING
    plan: ScrapingPlan | None = None
    preview_record: SellerLead | None = Field(
        default=None,
        description="Single sample record shown to the user for validation before full crawl.",
    )
    preview_record_css: SellerLead | None = Field(
        default=None,
        description="Preview record extracted via CSS selectors.",
    )
    preview_record_smart: SellerLead | None = Field(
        default=None,
        description="Preview record extracted via SmartScraperGraph (AI).",
    )
    preview_record_crawl4ai: SellerLead | None = Field(
        default=None,
        description="Preview record extracted via Crawl4AI (AI/Markdown).",
    )
    preview_record_universal_scraper: SellerLead | None = Field(
        default=None,
        description="Preview record extracted via universal-scraper (AI/BS4).",
    )
    preview_recommendation: str | None = Field(
        default=None,
        description="LLM explanation of which extraction method is better.",
    )
    preview_recommended_method: ExtractionMethod | None = Field(
        default=None,
        description="LLM-recommended extraction method.",
    )
    preview_items: list[dict] | None = Field(
        default=None,
        description="Multiple preview items (up to 10) shown during preview for better validation.",
    )
    preview_detail_record: dict | None = Field(
        default=None,
        description="Sample detail page data shown during plan review.",
    )
    extraction_method: ExtractionMethod | None = Field(
        default=None,
        description="User's chosen extraction method for the full crawl.",
    )
    user_feedback: str | None = Field(
        default=None,
        description="User feedback provided during preview (e.g. 'I also need email and phone').",
    )
    result: CrawlResult | None = None
    error: str | None = None
    quality_report: dict | None = Field(
        default=None,
        description="Quality assessment of extracted data (scores, coverage, recommendations).",
    )
    platform_info: dict | None = Field(
        default=None,
        description="Detected platform/technology stack of the target website.",
    )
    progress: dict | None = Field(
        default=None,
        description="Real-time progress information (items scraped, pages processed, etc.).",
    )
    diagnostics: JobDiagnostics = Field(
        default_factory=JobDiagnostics,
        description="Structured reliability diagnostics for confidence, failures, and status timeline.",
    )
    template_hints: TemplateHints | None = Field(
        default=None,
        exclude=True,
        description="Structural hints from a template, used internally by the planner.",
    )
    # Resume / partial-result tracking
    scraped_detail_urls: list[str] = Field(
        default_factory=list,
        description="Detail page URLs that were successfully fetched (for resume tracking).",
    )
    pending_detail_urls: list[str] = Field(
        default_factory=list,
        description="Detail page URLs that still need to be fetched (populated on timeout/partial).",
    )
    resume_from_job_id: str | None = Field(
        default=None,
        description="ID of the original job this resume job continues from.",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# ScrapeGraphAI tool requests / responses
# ---------------------------------------------------------------------------
class SmartScrapeMultiRequest(BaseModel):
    urls: list[str] = Field(description="List of URLs to scrape.")
    prompt: str = Field(description="Natural language prompt describing what to extract.")


class ScriptCreatorRequest(BaseModel):
    url: str = Field(description="URL to generate a scraping script for.")
    prompt: str = Field(description="Natural language prompt describing what to extract.")
    library: str = Field(
        default="beautifulsoup4",
        description="Python library for the generated script (beautifulsoup4, scrapy, etc.).",
    )
    auto_execute: bool = Field(
        default=True,
        description="Automatically execute the generated script and return results.",
    )


class ScriptCreatorMultiRequest(BaseModel):
    urls: list[str] = Field(description="List of URLs to generate a merged scraping script for.")
    prompt: str = Field(description="Natural language prompt describing what to extract.")
    library: str = Field(
        default="beautifulsoup4",
        description="Python library for the generated script (beautifulsoup4, scrapy, etc.).",
    )
    auto_execute: bool = Field(
        default=True,
        description="Automatically execute the generated script and return results.",
    )


class SmartScrapeResult(BaseModel):
    result: dict | list | str = Field(description="Extracted data from the scraped pages.")


class ScriptExecutionResult(BaseModel):
    stdout: str = Field(default="", description="Script stdout output.")
    stderr: str = Field(default="", description="Script stderr output.")
    returncode: int = Field(description="Process exit code.")
    timed_out: bool = Field(default=False, description="Whether the script timed out.")
    safety_warnings: list[str] = Field(
        default_factory=list,
        description="Safety warnings that blocked execution.",
    )


class ScriptResult(BaseModel):
    script: str = Field(description="Generated Python scraping script.")
    execution: ScriptExecutionResult | None = Field(
        default=None,
        description="Results from auto-executing the script. None if execution was skipped.",
    )


# ---------------------------------------------------------------------------
# Router agent — intelligent scraping method selection
# ---------------------------------------------------------------------------
class ScrapingStrategy(str, enum.Enum):
    FULL_PIPELINE = "full_pipeline"
    SMART_SCRAPER = "smart_scraper"
    SMART_SCRAPER_MULTI = "smart_scraper_multi"
    SCRIPT_CREATOR = "script_creator"


class RoutingDecision(BaseModel):
    strategy: ScrapingStrategy
    explanation: str = Field(description="Why this strategy was chosen.")


class SmartCrawlRequest(BaseModel):
    urls: list[str] = Field(min_length=1, description="One or more URLs to scrape.")
    prompt: str = Field(description="What data to extract.")
    fields_wanted: str | None = Field(
        default=None,
        description="Comma-separated list of fields to extract.",
    )
    item_description: str | None = Field(
        default=None,
        description="Describes what each item on the page represents.",
    )
    site_notes: str | None = Field(
        default=None,
        description="Any observations about the site that might help scraping.",
    )
    detail_page_url: str | None = Field(
        default=None,
        description="Example detail page URL for the planner.",
    )
    pagination_type: str | None = Field(
        default=None,
        description=(
            "User-specified pagination strategy. One of: "
            "'none', 'infinite_scroll', 'load_more_button', "
            "'next_button', 'page_numbers', 'alphabet_tabs'. "
            "If omitted the planner will auto-detect."
        ),
    )
    max_items: int | None = Field(
        default=None,
        description="Maximum number of items to scrape (for testing). None means no limit.",
    )
    max_pages: int | None = Field(
        default=None,
        description="Maximum number of pages to scrape. None means use global default.",
    )
    test_single: bool = Field(
        default=False,
        description="If true, only scrape and output a single item for testing.",
    )
    page_type: str | None = Field(
        default=None,
        description=(
            "What kind of page: 'directory', 'product_listing', 'event', or None for auto."
        ),
    )
    rendering_type: str | None = Field(
        default=None,
        description=(
            "How the page is built: 'static', 'dynamic', or None for auto-detect."
        ),
    )
    detail_page_type: str | None = Field(
        default=None,
        description=(
            "Detail page behavior: 'separate_page', 'none', 'popup_overlay', or None for auto-detect."
        ),
    )


class SmartCrawlResult(BaseModel):
    strategy_used: str = Field(description="The scraping strategy that was selected.")
    strategy_explanation: str = Field(description="Why this strategy was chosen.")
    data: dict | list | str | None = Field(
        default=None, description="Extracted data (for smart_scraper strategies).",
    )
    script: str | None = Field(
        default=None, description="Generated script (for script_creator strategy).",
    )
    execution: ScriptExecutionResult | None = Field(
        default=None, description="Script execution results.",
    )
    job_id: str | None = Field(
        default=None, description="Job ID for full_pipeline (async tracking).",
    )
