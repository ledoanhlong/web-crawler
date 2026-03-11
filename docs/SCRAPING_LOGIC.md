# Scraping Logic — Detailed Technical Documentation

This document provides a comprehensive explanation of how the web crawler's scraping logic works, from initial URL submission through to final structured output.

---

## Table of Contents

1. [High-Level Pipeline](#1-high-level-pipeline)
2. [Stage 1: Routing (RouterAgent)](#2-stage-1-routing-routeragent)
3. [Stage 2: Planning (PlannerAgent)](#3-stage-2-planning-planneragent)
4. [Stage 3: Scraping (ScraperAgent)](#4-stage-3-scraping-scraperagent)
5. [Stage 4: Parsing (ParserAgent)](#5-stage-4-parsing-parseragent)
6. [Stage 5: Output (OutputAgent)](#6-stage-5-output-outputagent)
7. [Template System](#7-template-system)
8. [Extraction Methods](#8-extraction-methods)
9. [Detail Page Enrichment](#9-detail-page-enrichment)
10. [Pagination Strategies](#10-pagination-strategies)
11. [Crawl4AI & universal-scraper Integration](#11-crawl4ai--universal-scraper-integration)
12. [Error Handling & Fallbacks](#12-error-handling--fallbacks)

---

## 1. High-Level Pipeline

When a user submits a URL and prompt, the system follows this pipeline:

```
User Request
     │
     ▼
RouterAgent ──► selects strategy (full_pipeline / smart_scraper / script_creator)
     │
     ▼ (if full_pipeline)
PlannerAgent ──► analyses HTML, produces ScrapingPlan
     │
     ▼
ScraperAgent ──► executes plan, returns raw PageData
     │    ├── Crawl4AI fetching (if enabled)
     │    ├── CSS Selector extraction
     │    ├── ScrapeGraphAI extraction
   │    ├── universal-scraper extraction (optional)
   │    ├── Claude extraction fallback (optional)
     │    ├── Detail page fetching (Crawl4AI, Playwright, or httpx)
     │    └── API interception
     │
     ▼
ParserAgent ──► normalizes raw data into SellerLead records
     │    └── universal-scraper detail extraction (optional)
     │
     ▼
OutputAgent ──► quality pass (dedup), writes JSON + CSV
```

The pipeline has a **preview checkpoint**: after planning, the system scrapes a single item using up to six extraction sources (CSS, ScrapeGraphAI, Crawl4AI, universal-scraper, listing API interception, Claude), parses them, and pauses for user review. Only after the user confirms does the full crawl proceed.

---

## 2. Stage 1: Routing (RouterAgent)

**File:** `app/agents/router_agent.py`

The `RouterAgent` receives the user's URL(s) and prompt, then decides which scraping strategy to use. It sends the request details to the LLM and expects a JSON response with `strategy` and `explanation`.

### Available Strategies

| Strategy | When Used | Description |
|----------|-----------|-------------|
| `full_pipeline` | Listing pages with pagination, detail pages | Full multi-agent pipeline with preview workflow |
| `smart_scraper` | Single page, simple extraction | Direct ScrapeGraphAI extraction (no CSS planning) |
| `smart_scraper_multi` | Multiple URLs, similar structure | ScrapeGraphAI across multiple URLs with merged results |
| `script_creator` | Complex pages, reusable scripts | Generates a Python script, optionally auto-executes |

### Fallback Routing

If the LLM call fails, a rule-based fallback selects the strategy:
- Multiple URLs → `smart_scraper_multi`
- Detail page URL or fields specified → `full_pipeline`
- Default → `smart_scraper`

---

## 3. Stage 2: Planning (PlannerAgent)

**File:** `app/agents/planner_agent.py`

The `PlannerAgent` is the most critical stage. It analyses the target page's HTML structure and produces a `ScrapingPlan` — a complete set of instructions for the scraper.

### Step-by-Step Flow

1. **Fetch the listing page**
   - Tries `httpx` (static HTTP) first
   - Falls back to Playwright if:
     - URL contains hash routing (`#/` or `#!/`)
     - HTML is too short (`<2000` chars)
     - Contains `<noscript>` tags
     - Has SPA framework markers (`id="app"`, `id="root"`, `ng-app`, etc.)
     - Body text is `<200` characters (JS shell)

2. **Optionally fetch a user-provided detail page**
   - Uses the same static-then-JS fallback logic

3. **Simplify the HTML**
   - Strips `<script>`, `<style>`, `<noscript>`, `<svg>`, `<iframe>`
   - Removes HTML comments, header, footer, nav, cookie banners
   - Collapses whitespace and truncates to 80k chars
   - In "aggressive" mode (retry after content filter): also strips images, data attributes, inline styles, ad containers, and the entire `<head>`

4. **LLM analysis**
   - Sends the simplified HTML to GPT with the `planner_listing` system prompt
   - Requests JSON output containing:
     - `item_container_selector` — CSS selector for each repeating item
     - `field_selectors` — map of field name → CSS selector (relative to container)
     - `field_attributes` — map of field name → HTML attribute to read
     - `pagination` strategy and selector
     - `requires_javascript` flag
     - `detail_link_selector` — link to detail pages
     - `detail_button_selector` — JS-only button (no href, triggers XHR)
   - If the content filter triggers, retries with aggressive HTML sanitization

5. **Sanitize LLM output**
   - Flattens nested dicts in `detail_page_fields`, `field_selectors`, etc.
   - Strips non-string values

6. **Heuristic override**
   - If the httpx fetch was insufficient (detected JS-rendered page), forces `requires_javascript=True` even if the LLM said otherwise

7. **Detail page analysis** (if `detail_link_selector` found)
   - Extracts the first detail link from the listing HTML
   - Fetches that detail page
   - Sends it to GPT with the `planner_detail` prompt
   - Produces a `DetailPagePlan` with its own `field_selectors` and `sub_links`

8. **Detail API discovery** (if `detail_button_selector` found, no detail page)
   - Uses Playwright to click the first item's detail button
   - Intercepts all JSON network responses after the click
   - Scores each response to find the most likely detail API (filters noise: analytics, chat widgets, trackers)
   - Sends the captured API URL and response to GPT with the `planner_detail_api` prompt
   - Produces a `DetailApiPlan` with `api_url_template`, `id_selector`, `id_attribute`, `id_regex`

9. **Vision fallback planning** (optional)
   - If selector quality remains poor after retries (low container count, low hit ratio, weak content test), planner captures a page screenshot and sends screenshot + simplified HTML to GPT-4o.
   - If vision plan validates better than text-only plan, it replaces the original plan.

### ScrapingPlan Schema (key fields)

```
ScrapingPlan:
  url: str                          # Target listing URL
  requires_javascript: bool         # Whether Playwright is needed
  pagination: PaginationStrategy    # none, next_button, page_numbers, etc.
  pagination_selector: str          # CSS selector for pagination control
  pagination_urls: list[str]        # Pre-computed page URLs
  alphabet_tab_selector: str        # CSS selector for A-Z tabs
  target:
    item_container_selector: str    # CSS selector for each item wrapper
    field_selectors: dict           # field_name → CSS selector
    field_attributes: dict          # field_name → HTML attribute
    detail_link_selector: str       # Link to detail page
    detail_button_selector: str     # JS-only detail button
  detail_page_plan: DetailPagePlan  # Detail page CSS selectors
  detail_api_plan: DetailApiPlan    # API interception configuration
  wait_selector: str                # Wait for this element before scraping
```

---

## 4. Stage 3: Scraping (ScraperAgent)

**File:** `app/agents/scraper_agent.py`

The `ScraperAgent` executes the `ScrapingPlan` and returns a list of `PageData` objects containing raw extracted items.

### Execution Path Selection

The scraper selects its execution path based on the plan and configuration:

```
Crawl4AI enabled + compatible pagination?  →  _scrape_crawl4ai()
plan.pagination == API_ENDPOINT?            →  _scrape_api()
plan.requires_javascript?                   →  _scrape_js()
settings.use_scrapy?                        →  _scrape_scrapy() (with httpx fallback)
otherwise                                   →  _scrape_static()
```

### 4.1 Crawl4AI Path (`_scrape_crawl4ai`)

Uses Crawl4AI, a local async crawler, for page fetching. Enabled when `USE_CRAWL4AI=true`. Compatible with pagination strategies that have pre-computed URLs (`none`, `page_numbers`).

1. Resolves page URLs from `plan.pagination_urls` or just the base URL
2. Fetches all pages via `crawl4ai_fetch_batch()` (concurrent async fetching)
3. Crawl4AI handles JS rendering locally using an embedded browser
4. For each page, extracts items using the existing `_extract_items_with_fallback()` pipeline (CSS selectors on the returned HTML)
5. Falls back to Playwright or httpx if Crawl4AI returns no results for any URLs
6. Runs detail page enrichment (also via Crawl4AI when enabled)

**Why only for `none` and `page_numbers`?** Interactive pagination strategies (infinite scroll, load-more, next button, alphabet tabs) require maintaining browser state across multiple interactions. Crawl4AI fetches each URL independently and can't replicate the "click-and-wait-repeat" loop. These strategies continue to use Playwright.

### 4.2 universal-scraper Path (`_scrape_universal_scraper`)

Uses universal-scraper for AI-powered extraction. Enabled when `USE_UNIVERSAL_SCRAPER=true`. This method generates BeautifulSoup extraction code via AI, caches it for reuse, and applies it to pages.

1. Resolves page URLs from `plan.pagination_urls` or just the base URL
2. Pages are fetched via Crawl4AI, Playwright, or httpx (depending on configuration)
3. universal-scraper generates and caches BS4 extraction code for the page structure
4. For each page, applies the generated extraction code to get structured items
5. Falls back to CSS or ScrapeGraphAI extraction if universal-scraper returns no results

### 4.3 Static Path (`_scrape_static`)

Uses `httpx` for fast, concurrent HTTP requests:

1. Resolves page URLs from `plan.pagination_urls` or just the base URL
2. Fetches all pages concurrently (respecting `max_concurrent_requests` semaphore)
3. For each page HTML, calls `_extract_items_with_fallback()` to get items
4. Enriches items with detail pages and API data

### 4.4 JavaScript Path (`_scrape_js`)

Uses Playwright for pages that require browser rendering:

| Pagination | Implementation |
|------------|---------------|
| `ALPHABET_TABS` | `click_all_tabs()` — clicks each A-Z tab, captures HTML per tab. Supports compound pagination (tabs + numbered pages within each tab) |
| `INFINITE_SCROLL` | Opens a single page, calls `scroll_to_bottom()` repeatedly until no new content loads |
| `LOAD_MORE_BUTTON` | Opens a single page, clicks the "Load More" button repeatedly until it disappears |
| `NEXT_BUTTON` | Opens a page, extracts items, clicks "Next", repeats |
| Default | Navigates to pre-resolved page URLs one by one |

#### Consent Banner Dismissal

Before interacting with page elements, the scraper dismisses cookie consent overlays:
- **Shadow DOM** consent managers (Usercentrics, etc.) — searches inside `#usercentrics-root` shadow root for accept buttons
- **Regular DOM** consent selectors — tries common CSS patterns like `[class*='cookie'] [class*='accept']`, `#onetrust-accept-btn-handler`, etc.

#### Compound Pagination (Tabs + Pages)

For sites with alphabet tabs that also have numbered pagination within each tab:
1. If `pagination_urls` are pre-computed by the planner, navigates them directly
2. Otherwise, auto-detects inner pagination by searching for numbered links using common CSS patterns (`.pagination a`, `.pager a`, etc.)
3. Extrapolates missing page URLs by detecting the varying query parameter pattern (e.g., `?page=1` → `?page=2` → ... → `?page=N`)

### 4.5 Scrapy Path (`_scrape_scrapy`)

Runs the Scrapy `PlanSpider` in a **subprocess** to avoid Twisted/asyncio reactor conflicts:

1. Serializes the `ScrapingPlan` to a temp JSON file
2. Passes Scrapy settings through environment variables
3. Launches `python -m app.scrapy_runner.run` as a subprocess
4. The `PlanSpider` uses the plan's CSS selectors, handles pagination, and follows detail links
5. Items are collected via `ItemCollectorPipeline` and written to a temp JSON file
6. The parent process reads the output and converts it to `PageData`

Scrapy is only used for compatible pagination strategies: `none`, `next_button`, `page_numbers`. JS-heavy strategies (scroll, tabs, load-more) always use Playwright.

### 4.6 API Path (`_scrape_api`)

For sites with a discovered JSON API endpoint:

1. Sends GET requests to `plan.api_endpoint` with `plan.api_params`
2. Paginates by incrementing a `page` parameter
3. Searches response JSON for list data under common wrapper keys (`data`, `items`, `results`, etc.)
4. Converts JSON items to string-value dicts for downstream parsing

### 4.7 Item Extraction (`_extract_items`)

### 4.8 Claude Fallback Extraction

When CSS + Smart extraction fail (or when method `claude` is selected), scraper calls Claude Opus 4.6 through Azure AI Foundry's Anthropic Messages endpoint.

Key behaviors:
- Uses simplified HTML + expected field list.
- Returns JSON records and extraction notes.
- Captures provider metadata for telemetry: latency, input/output tokens, estimated cost.
- Controlled by policy flags:
   - `CLAUDE_FALLBACK_ONLY` (OpenAI-first default)
   - `CLAUDE_MAX_RETRIES_PER_STAGE`
   - circuit-breaker settings.

### 4.9 Provider Health and Telemetry

- Startup pings providers (OpenAI, Vision, Claude).
- `/health` includes provider readiness and runtime circuit-breaker state.
- Per-job provider telemetry is exposed via:
   - `GET /api/v1/crawl/{job_id}/telemetry`
   - includes fallback reasons, latency, and estimated token/cost metrics.

The core CSS extraction logic:

```python
for container in soup.select(item_container_selector):
    record = {}
    for field, selector in field_selectors.items():
        # Skip empty selectors defensively
        if not selector or not selector.strip():
            record[field] = None
            continue
        el = container.select_one(selector)
        if el:
            attr = field_attributes.get(field)
            record[field] = el.get(attr) if attr else el.get_text(strip=True)
    # Also extract detail_link and _detail_api_id if configured
    items.append(record)
```

Key defensive measures:
- Empty/whitespace-only CSS selectors are skipped (prevents `ValueError`)
- Invalid CSS selectors are caught and logged (field set to `None`)
- Detail link and API ID selectors have the same defensive checks

### 4.8 Preview (`scrape_preview_dual`)

During the preview stage, the scraper runs **up to four** extraction methods on the same page:

1. Fetches the page once (httpx, Crawl4AI, or Playwright depending on configuration and `requires_javascript`)
2. Runs **CSS extraction** via `_extract_items()` on the HTML
3. Runs **ScrapeGraphAI extraction** via `smart_extract_items()` on the HTML (if `USE_SMART_SCRAPER_PRIMARY=true` and HTML is ≥ 500 chars)
4. Runs **Crawl4AI extraction** via `crawl4ai_extract()` on the page (if `USE_CRAWL4AI=true`)
5. Runs **universal-scraper extraction** via `universal_scraper_extract()` on the page (if `USE_UNIVERSAL_SCRAPER=true`)
6. Returns a 4-tuple `(css_pages, smart_pages, crawl4ai_pages, universal_scraper_pages)` — each containing at most 1 item
7. Each non-empty result set is enriched with detail page data (1 detail page max)

If CSS extraction finds 0 items with httpx, it retries with Playwright (the page may need JS rendering).

The orchestrator then parses each result set through the `ParserAgent`, collects all methods that produced results into a `candidates` dict, and uses an LLM comparison to recommend the best extraction method. The user can accept the recommendation or choose a different method.

---

## 5. Stage 4: Parsing (ParserAgent)

**File:** `app/agents/parser_agent.py`

The `ParserAgent` normalizes raw scraped data into structured `SellerLead` records.

### Enrichment Merging

Before sending to the LLM, the parser builds enriched items:

1. **Detail page data** — For items with a `detail_link`, the parser extracts fields from the detail page HTML using a cascading strategy (first successful result wins):
   1. **universal-scraper** — If `USE_UNIVERSAL_SCRAPER=true`, calls universal-scraper's AI-powered extraction on the detail page. Generates and caches BS4 extraction code for the page structure.
   2. **ScrapeGraphAI** — If `USE_SMART_SCRAPER_PRIMARY=true`, runs LLM-based extraction on the pre-fetched HTML.
   3. **CSS selectors** — Uses `detail_page_plan.field_selectors` (or legacy `detail_page_fields`) with BeautifulSoup.
   4. **Simplified HTML** — As a last resort, sends a truncated (4,000 chars) simplified version of the HTML for the LLM to interpret during batch parsing.

2. **Detail API data** — For items with an `_detail_api_id`, attaches the compact API response JSON (stripped of metadata keys like `_links`, `__typename`, `lastModified`, etc.; truncated long text fields to 1,500 chars; large lists capped at 20 items)

3. **Sub-page data** — For detail pages with followed sub-links (e.g., "Products" tab), appends the simplified sub-page HTML

4. **Structured data** — Page-level JSON-LD, Open Graph, and Microdata signals are attached to each item (if compact representation < 3,000 chars), giving the parser LLM extra context

### Batch Processing

Items are split into size-aware batches (`_MAX_BATCH_CHARS = 60,000`) to stay within LLM context limits. Each batch is sent to GPT with the `parser` system prompt, which instructs the LLM to:

- Map messy field names to the canonical `SellerLead` schema
- Split combined fields (e.g., "Berlin, Germany" → city + country)
- Clean up whitespace, HTML entities, encoding artifacts
- Merge listing + detail page data
- Return a JSON array of `records`

If a batch fails (e.g., content filter), it's recursively split in half and retried.

---

## 6. Stage 5: Output (OutputAgent)

**File:** `app/agents/output_agent.py`

### LLM Quality Pass

If there are 3+ records, the output agent sends them through a final GPT quality pass:

- Deduplication (e.g., same company listed twice with slightly different names)
- Consistency fixes (e.g., normalize phone formats, fix email casing)
- Enrichment of incomplete records

Records are chunked into 60k-char batches for the LLM call. If a chunk fails, the original records are kept.

### File Output

1. **JSON** — Pretty-printed array of `SellerLead` dicts, excluding `None` fields
2. **CSV** — Flattened table with social media as separate columns (`social_facebook`, etc.) and raw_extra fields as `extra_*` columns

Files are written to `output/{job_id}/results.json` and `results.csv`.

---

## 7. Template System

**File:** `app/services/template_loader.py`

Templates describe common **website patterns** using structural hints. They do NOT contain site-specific CSS selectors — the AI planner always runs and generates selectors from the actual target page.

### How Templates Work

1. Templates are loaded from `app/templates/*.json` on first access (cached)
2. When a template is selected, `get_hints_from_template()` returns a `TemplateHints` object
3. The orchestrator passes these hints to `PlannerAgent`, which uses them as structural guidance alongside the actual page HTML
4. The planner always generates CSS selectors by analyzing the real page — hints just tell it what kind of structure to expect

### Template JSON Structure

```json
{
  "id": "dynamic-directory-detail-pages",
  "name": "JS Directory + Detail Pages",
  "description": "JavaScript-rendered directory with alphabet tabs and separate detail pages",
  "platform": "generic",
  "default_prompt": "Extract all company details...",
  "default_fields_wanted": "name, address, phone, email, website, ...",
  "hints": {
    "requires_javascript": true,
    "pagination": "alphabet_tabs",
    "has_detail_pages": true,
    "has_detail_api": false,
    "notes": "Each company links to a separate detail page with full contact info"
  }
}
```

### Included Templates

#### dynamic-directory-detail-pages (JS Directory + Detail Pages)
- **Pattern:** A-Z alphabet tabs, JS-rendered, each item links to a separate detail page
- **Hints:** `requires_javascript=true`, `pagination=alphabet_tabs`, `has_detail_pages=true`
- **Use for:** Sites like Koelnmesse exhibitor directories where each company has its own profile page

#### dynamic-directory-api (JS Directory + API Details)
- **Pattern:** A-Z alphabet tabs, JS-rendered, detail data loaded via hidden XHR/API calls
- **Hints:** `requires_javascript=true`, `pagination=alphabet_tabs`, `has_detail_api=true`
- **Use for:** Sites where clicking an item loads detail data from an API endpoint without navigating to a new page

#### static-listing (Static HTML Listing)
- **Pattern:** Simple server-rendered HTML list, all data on one page
- **Hints:** `requires_javascript=false`, `pagination=none`, no detail pages or API
- **Use for:** WordPress-style single-page exhibitor lists, simple HTML tables

### Creating New Templates

1. Identify the website **pattern** (how is data paginated? JS required? separate detail pages?)
2. Create a JSON file in `app/templates/` with structural `hints`
3. Do NOT include CSS selectors — the planner generates those from the real page HTML

---

## 8. Extraction Methods

### CSS Selectors (Default)

Fast, deterministic extraction using BeautifulSoup `select_one()`:
- Each field has a CSS selector relative to the item container
- Attributes like `href`, `src` can be read instead of text content
- Supports comma-separated selector fallbacks (e.g., `h3.name, .name`)

### ScrapeGraphAI (AI Extraction)

Uses LLM-powered extraction via `SmartScraperGraph`:
- Accepts raw HTML as source (no extra HTTP request)
- Builds a natural language prompt asking for specific fields
- Returns structured JSON data
- 120-second timeout to prevent hanging
- Used as fallback when CSS selectors find fewer than 3 items

### Crawl4AI (Local Async Crawling)

Uses Crawl4AI, a local async crawler that outputs markdown:
- Runs locally — no external API calls for fetching
- Handles JS-rendered pages via an embedded browser
- Returns markdown output suitable for LLM-based extraction
- Enabled via `USE_CRAWL4AI=true`
- Results are normalized to the same field text format as CSS and SmartScraper for seamless integration with the parser
- Available for both listing page fetching and preview extraction

### universal-scraper (AI-Powered Extraction)

Uses universal-scraper for AI-powered BeautifulSoup code generation:
- Generates BS4 extraction code via AI, then caches it for reuse on similar pages
- Works on pre-fetched HTML — does not perform its own fetching
- Caching means subsequent pages with the same structure are extracted without additional AI calls
- Enabled via `USE_UNIVERSAL_SCRAPER=true`
- Results are normalized to the same field text format as CSS and SmartScraper for seamless integration with the parser
- Available for listing page preview, full extraction, and detail page enrichment

### Multi-Method Preview Comparison

During preview, up to four methods run on the same page. The orchestrator collects all methods that produced results into a `candidates` dict and uses an LLM comparison call to recommend the best method based on:
- Completeness (non-null fields)
- Accuracy (clean values)
- Coverage (useful information)

The comparison is dynamic — it works with any combination of 2, 3, or 4 methods (e.g., if Crawl4AI or universal-scraper is disabled or fails, it falls back to fewer-way comparisons). If only one method produces results, it's selected automatically without an LLM call.

---

## 9. Detail Page Enrichment

### Four Approaches

1. **Detail Page HTML** — Follow a link to each item's profile page, extract fields via Crawl4AI, universal-scraper, CSS, or AI
2. **Detail API Interception** — Click a JS-only button, capture the XHR response, template the API URL
3. **Sub-Links** — Follow links on detail pages (e.g., "Products", "Contact") for additional data
4. **universal-scraper Detail Extraction** — Use universal-scraper's AI-powered extraction on the detail page HTML (generates and caches BS4 code for the page structure)

### Detail Page Fetching

Detail page fetching is the highest-volume fetch operation (one request per item). The scraper selects the fetching method based on configuration:

```
USE_CRAWL4AI enabled?
    → Crawl4AI batch fetch (crawl4ai_fetch_batch)
      - Concurrent async fetching
      - Local JS rendering via embedded browser
      - Falls back to Playwright/httpx for any URLs Crawl4AI misses

plan.requires_javascript?
    → Playwright (fetch_page_js)
      - Uses browser context for JS-rendered pages

otherwise
    → httpx (fetch_pages)
      - Fast static HTTP fetches
```

Detail pages are fetched in batches of 10. Between batches, the cancel event is checked to allow graceful timeout with partial results.

### API Interception Flow

1. Navigate to listing page with Playwright
2. Find the first item container
3. Find the detail button within it
4. Dismiss consent overlays
5. Clear network captures
6. Click the button (JS `el.click()` for reliability)
7. Wait for network idle + 2s buffer
8. Score all captured JSON responses:
   - **Penalty:** URLs containing noise fragments (analytics, tracking, chat, cookie, etc.)
   - **Bonus:** Response keys matching detail data patterns (name, address, phone, etc.)
   - **Bonus:** URL containing keywords like "seller", "profile", "detail"
9. Select the highest-scoring response
10. LLM derives the URL template (replacing the specific ID with `{id}`) and the CSS selector + regex to extract item IDs from the listing page

### API Detail Fetching

For JS-rendered sites, API calls use **Playwright's browser context** (same session cookies):
1. Navigate to the listing page to establish session
2. Use `fetch()` inside the browser context with `credentials: "include"`
3. This ensures auth tokens and cookies are automatically included

For static sites, `httpx` is used with browser-like headers and a `Referer` header.

---

## 10. Pagination Strategies

| Strategy | Trigger | Implementation |
|----------|---------|---------------|
| `none` | Single-page listings | Fetch one URL |
| `page_numbers` | Numbered page links | Pre-computed URLs (from planner or extrapolated) |
| `next_button` | "Next" button | Click repeatedly until button disappears |
| `infinite_scroll` | Lazy-loaded content | Scroll to bottom repeatedly until height stops changing |
| `load_more_button` | "Load More" button | Click repeatedly until button disappears |
| `alphabet_tabs` | A-Z letter tabs | Click each tab, optionally detect inner pagination |
| `api_endpoint` | Discovered JSON API | Direct HTTP GET with page parameter |

### URL Extrapolation for Page Numbers

When only some page links are visible (e.g., pages 1-10 out of 80):
1. Parse page 1 and page 2 URLs
2. Find the query parameter that changes (e.g., `?offset=0` → `?offset=20`)
3. Calculate the step size
4. Generate all URLs up to the maximum page number

---

## 11. Crawl4AI & universal-scraper Integration

**Files:** `app/utils/crawl4ai.py`, `app/utils/universal_scraper.py`, `app/config.py`

Crawl4AI and universal-scraper are optional integrations that provide local async crawling and AI-powered extraction respectively. Both are controlled by feature flags and designed with graceful fallbacks — all functions return `None` on failure, allowing callers to fall back to existing methods.

### Crawl4AI

Crawl4AI is a local async crawler that fetches pages and produces markdown output, suitable for downstream LLM-based extraction. It runs entirely locally (no external API calls for fetching) and uses an embedded browser for JS rendering.

**File:** `app/utils/crawl4ai.py`

| Function | Purpose |
|----------|---------|
| `crawl4ai_fetch(url)` | Fetch a single URL with local JS rendering. Returns markdown and HTML content |
| `crawl4ai_fetch_batch(urls)` | Fetch multiple URLs concurrently. Returns `{url: result}` dict |
| `crawl4ai_extract(url, prompt)` | Fetch and extract structured data from a URL. Returns extracted data dict |

### universal-scraper

universal-scraper provides AI-powered extraction by generating BeautifulSoup code via AI and caching it for reuse. Once extraction code is generated for a page structure, subsequent pages with the same structure are extracted without additional AI calls, making it efficient for large crawls.

**File:** `app/utils/universal_scraper.py`

| Function | Purpose |
|----------|---------|
| `universal_scraper_extract(html, prompt)` | Generate and apply BS4 extraction code on HTML. Returns structured data. Caches generated code for reuse |
| `universal_scraper_extract_batch(html_list, prompt)` | Apply cached extraction code to multiple HTML pages. Returns list of extracted data |
| `universal_scraper_extract_detail(html, prompt)` | Extract structured data from a detail page HTML. Uses cached code when available |

### Configuration

Both features are off by default. To enable, set environment variables:

```env
USE_CRAWL4AI=true                      # Enable Crawl4AI for page fetching
USE_UNIVERSAL_SCRAPER=true             # Enable universal-scraper for AI-powered extraction
```

### Where Crawl4AI & universal-scraper Are Used

```
┌─────────────────────────────────────────────────────────────────────┐
│  ScraperAgent.scrape()                                              │
│  └─ _scrape_crawl4ai()  →  crawl4ai_fetch_batch()                  │
│     Fetches listing pages for none/page_numbers pagination          │
│     Flag: USE_CRAWL4AI                                              │
│                                                                     │
│  ScraperAgent._enrich_detail_pages()  →  crawl4ai_fetch_batch()     │
│     Fetches detail pages with local JS rendering                    │
│     Falls back to Playwright/httpx for missed URLs                  │
│     Flag: USE_CRAWL4AI                                              │
│                                                                     │
│  ScraperAgent.scrape_preview_dual()                                 │
│     Runs Crawl4AI extraction as a preview method                    │
│     Flag: USE_CRAWL4AI                                              │
│                                                                     │
│     Runs universal-scraper extraction as a preview method           │
│     Flag: USE_UNIVERSAL_SCRAPER                                     │
├─────────────────────────────────────────────────────────────────────┤
│  ParserAgent._extract_detail_fields()                               │
│     Extracts structured data from detail page HTML                  │
│     Falls back to SmartScraper → CSS if empty                       │
│     Flag: USE_UNIVERSAL_SCRAPER                                     │
├─────────────────────────────────────────────────────────────────────┤
│  Orchestrator.run_preview()                                         │
│     Includes Crawl4AI and universal-scraper results in comparison   │
│     Dynamic N-way LLM comparison (2, 3, or 4 methods)              │
│     No separate flags — follows respective enable flags             │
└─────────────────────────────────────────────────────────────────────┘
```

### Pagination Compatibility

Crawl4AI fetches each URL independently and can't maintain browser state across interactions. This means:

| Pagination Strategy | Crawl4AI Compatible? | Reason |
|--------------------|-----------------------|--------|
| `none` | Yes | Single URL, no interaction needed |
| `page_numbers` | Yes | Pre-computed URLs, each fetched independently |
| `next_button` | No | Requires clicking in a persistent browser session |
| `infinite_scroll` | No | Requires scrolling in a persistent browser session |
| `load_more_button` | No | Requires clicking in a persistent browser session |
| `alphabet_tabs` | No | Requires clicking tabs in a persistent browser session |
| `api_endpoint` | No (not needed) | Direct HTTP GET, no fetching needed |

For incompatible strategies, the scraper falls back to Playwright automatically. universal-scraper works on pre-fetched HTML regardless of how the page was fetched, so it has no pagination compatibility restrictions.

### Fallback Behavior

All Crawl4AI and universal-scraper functions return `None` on any exception. Callers always check for `None` and fall back to existing methods:

- **Page fetching:** `_scrape_crawl4ai()` falls back to `_scrape_js()` / `_scrape_static()` if Crawl4AI returns no results
- **Detail enrichment:** `_enrich_detail_pages()` falls back to Playwright or httpx for any URLs that Crawl4AI failed to fetch
- **Extraction:** `universal_scraper_extract_detail()` empty result falls back to SmartScraper, then CSS

---

## 12. Error Handling & Fallbacks

### Layered Fallback Strategy

```
Crawl4AI fetch fails → Playwright / httpx fallback
universal-scraper extraction empty → SmartScraper fallback → CSS fallback
httpx fetch fails → Playwright fetch
Scrapy fails → httpx fallback
CSS selectors find 0 items → retry with Playwright (if static fetch was used)
CSS selectors find <3 items → ScrapeGraphAI fallback
ScrapeGraphAI returns 0 → CSS results used
ScrapeGraphAI times out (120s) → CSS results used
LLM content filter triggers → retry with aggressive HTML sanitization
LLM parse batch fails → split batch in half and retry recursively
QA pass chunk fails → keep original records
```

### Crawl4AI & universal-scraper Fallback Design

All Crawl4AI and universal-scraper wrapper functions return `None` on any exception. Callers always check for `None` and fall back to existing methods:

- **Page fetching:** `_scrape_crawl4ai()` falls back to `_scrape_js()` / `_scrape_static()` if Crawl4AI returns no results
- **Detail enrichment:** `_enrich_detail_pages()` falls back to Playwright or httpx for any URLs that Crawl4AI failed to fetch
- **Extraction:** `universal_scraper_extract_detail()` empty result falls back to SmartScraper, then CSS

### Defensive CSS Selector Handling

- Empty or whitespace-only selectors are skipped (set field to `None`)
- Invalid CSS selectors are caught, logged, and skipped
- Applied consistently across field selectors, detail link selectors, and API ID selectors

### Timeout Protection

- HTTP requests: configurable `request_timeout_s` (default 30s)
- Playwright navigation: 120s hard timeout
- Playwright selector wait: 10-15s timeout
- ScrapeGraphAI extraction: 120s timeout
- Scrapy subprocess: configurable `scrapy_subprocess_timeout_s` (default 600s)
- Script execution: 60s sandbox timeout
- Detail page enrichment: 60s per-page timeout
- Crawl4AI: configurable timeouts for local browser operations

### Rate Limiting

- Configurable delay between requests (`request_delay_ms`)
- Concurrent request semaphore (`max_concurrent_requests`)
- Scrapy autothrottle with configurable start/max delay
- Per-domain concurrent request limits
- Crawl4AI batch fetching: concurrent async requests
